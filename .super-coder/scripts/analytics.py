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
When no window matches (rows predating lifecycle archives) but the cwd
resolves to exactly one shell, shell_id is still set — the shell mapping is
deterministic on its own, only the archive needs the window. Residual
ambiguity stays NULL — visible, flagged unattributed,
never guessed. cwd is transient (attribution runs within the sweep batch);
rows that stay NULL keep their spend visible in the totals regardless.

ended_at backfill: run.py execs the harness, so nothing can write the
archive's end time at exit — the sweep sets it to the last attributed
harness activity (refreshed while a session is still live).
"""
from __future__ import annotations

import json
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


def _load_cache(con, harness: str, parser_version: str) -> dict:
    """This harness's persisted parse cache (migration 0073) — {} on a
    version mismatch, corrupt payload, or a pre-0073 DB. The cache is a
    disposable accelerator: any miss just means a fuller re-parse."""
    try:
        r = con.execute(
            "SELECT parser_version, payload FROM analytics_parse_cache WHERE harness=?",
            (harness,)).fetchone()
    except db_driver.OperationalError:
        return {}
    if r and r["parser_version"] == parser_version:
        try:
            c = json.loads(r["payload"])
            if isinstance(c, dict):
                return c
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _save_cache(con, harness: str, parser_version: str, cache: dict) -> None:
    if not cache:
        return
    try:
        con.execute(
            "INSERT INTO analytics_parse_cache (harness, parser_version, payload, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(harness) DO UPDATE SET "
            "parser_version=excluded.parser_version, payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (harness, parser_version, json.dumps(cache, separators=(",", ":")),
             _now_iso()))
    except (db_driver.OperationalError, TypeError, ValueError):
        pass  # pre-0073 DB or unserializable state — never block the sweep


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
    # repo root (or an in-repo subdir outside the worktrees) → admin-flavor
    # shells; anything off-repo maps to nothing (parsers filter those out,
    # this is the defensive backstop)
    if cwd == root or cwd.startswith(root + "/"):
        return admins
    return []


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


def _attribute(con, batch: list[dict], log) -> "tuple[int, int]":
    by_wt, admins = _cwd_shells(con)
    windows = _windows(con)
    attributed = shell_only = 0
    for r in batch:
        if not r.get("cwd"):
            continue
        sids = _shell_for_cwd(r["cwd"], by_wt, admins)
        t = _iso_epoch(r.get("started_at") or r.get("ended_at"))
        hits = []
        if t is not None:
            for sid in sids:
                for aid, harness, start, end in windows.get(sid, []):
                    if harness == r["harness"] and start is not None \
                            and start <= t and (end is None or t < end):
                        hits.append((aid, sid))
        if len(hits) == 1:
            aid, sid = hits[0]
            cur = con.execute(
                "UPDATE session_token_usage SET archive_id=?, shell_id=? "
                "WHERE harness=? AND harness_session_ref=? AND model IS ? AND archive_id IS NULL",
                (aid, sid, r["harness"], r["harness_session_ref"], r["model"]))
            attributed += cur.rowcount
        elif len(sids) == 1:
            # No usable archive window (predates lifecycle rows, or ambiguous)
            # but the shell itself is deterministic from cwd alone — worktree
            # name IS the shortname; repo root maps to the sole admin shell.
            # Shell-level attribution keeps flavor rollups (favorite model)
            # fed by pre-lifecycle history; archive_id stays NULL so a later
            # window match can still land the full attribution.
            cur = con.execute(
                "UPDATE session_token_usage SET shell_id=? "
                "WHERE harness=? AND harness_session_ref=? AND model IS ? "
                "AND archive_id IS NULL AND shell_id IS NULL",
                (sids[0], r["harness"], r["harness_session_ref"], r["model"]))
            shell_only += cur.rowcount
        # else: cwd ambiguous (multiple admin shells) or off-repo — stays NULL
    return attributed, shell_only


def _backfill_ended(con) -> int:
    cur = con.execute(
        "UPDATE shell_memory_archives SET ended_at = ("
        "  SELECT MAX(u.ended_at) FROM session_token_usage u"
        "  WHERE u.archive_id = shell_memory_archives.archive_id AND u.ended_at IS NOT NULL) "
        "WHERE started_at IS NOT NULL AND archive_id IN "
        "  (SELECT DISTINCT archive_id FROM session_token_usage WHERE archive_id IS NOT NULL)")
    return cur.rowcount


def sweep(only: "str | None" = None, quiet: bool = False,
          full: bool = False) -> dict:
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
            since = (lambda ref: None) if full \
                else _since_fn(con, mod.HARNESS, mod.PARSER_VERSION)
            cache = {} if full else _load_cache(con, mod.HARNESS, mod.PARSER_VERSION)
            try:
                rows = mod.sweep(REPO_ROOT, since, log, cache=cache)
            except Exception as e:  # plugin stance: loud, never fatal to the sweep
                log(f"analytics: {mod.HARNESS} parser failed: {e!r}")
                continue
            _save_cache(con, mod.HARNESS, mod.PARSER_VERSION, cache)
            for r in rows:
                counts[_upsert(con, r, captured_at)] += 1
                batch.append(r)
        attributed, shell_only = _attribute(con, batch, log)
        ended = _backfill_ended(con)
        con.commit()
        summary = {"inserted": counts["insert"], "updated": counts["update"],
                   "attributed": attributed, "shell_attributed": shell_only,
                   "ended_backfilled": ended, "notes": notes}
        if not quiet:
            print(f"analytics: {counts['insert']} new, {counts['update']} refreshed, "
                  f"{attributed} attributed, {shell_only} shell-only, "
                  f"{ended} archive end(s) backfilled")
        return summary
    finally:
        con.close()


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: sc analytics sweep [--harness <name>] [--quiet] [--full]\n"
              "  parse each harness's on-disk usage data for THIS repo into\n"
              "  session_token_usage (incremental, idempotent)\n"
              "  --full ignores the incremental watermark and re-parses every\n"
              "  session — use once to backfill shell attribution on rows\n"
              "  swept before it existed")
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
    sweep(only=only, quiet=quiet, full="--full" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
