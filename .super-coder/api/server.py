#!/usr/bin/env python3
"""super-coder review layer — a zero-dependency localhost server.

One stdlib HTTP server serves both the JSON API and the static review UI on a
single per-fork port (see scripts/ports.py). No FastAPI, no venv, no build step:
a fork needs only python3 + sqlite3, which the install already requires. Single-
user, localhost — network controls are the operator's, exactly like superCC's
API surface.

It is a REVIEW layer over the live `shell_db.db`. The law-curated fields (seed,
L&S) are returned for reading but have **no write endpoint at all** — not a
disabled control, an absent route (Laws 2-4, 7; spec §GUI). Editable: a shell's
operational fields (current_state, connections, workspace) + skill grants;
roadmap rows; non-frozen documents; flags (create / resolve).

Run:
    python3 .super-coder/api/server.py [--port N]
    (defaults to the derived port from scripts/ports.py)
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
UI_DIR = ENGINE / "ui"

# Rolling webapp event log — visibility into what the API actually did, since a
# publish/snapshot that "looked done" gave no trace to inspect after the fact.
# ONE file, last LOG_MAX_EVENTS end-to-end events, JSON-per-line so it's both
# greppable and machine-parseable (the multi-line step trace rides in `detail`,
# keeping each event a single physical line so the roll is a line-count trim).
# Local + ephemeral: under the gitignored .super-coder/logs/, never committed.
LOG_DIR = ENGINE / "logs"
LOG_PATH = LOG_DIR / "webapp.log"
LOG_MAX_EVENTS = 20
_LOG_LOCK = threading.Lock()

sys.path.insert(0, str(ENGINE / "scripts"))
import git_hygiene  # noqa: E402  (live repo dirty/stale/clean snapshot)
import map_db  # noqa: E402  (read-only handle to the dr_* catalogue in map.db)
import ports as ports_mod  # noqa: E402
import shell_factory  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402  (engine_skill_names — origin rule)
import vm as vm_mod  # noqa: E402  (Windows Test VM — config + live checks)
import ts as ts_mod  # noqa: E402  (tailnet — config + live checks)

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    # vendored markdown pipeline (marked MIT, DOMPurify MPL-2.0/Apache-2.0) —
    # local copies so the no-build UI renders sanitized GFM without a CDN.
    "/vendor/marked.umd.js": ("vendor/marked.umd.js", "application/javascript; charset=utf-8"),
    "/vendor/purify.min.js": ("vendor/purify.min.js", "application/javascript; charset=utf-8"),
}

# md-converter inline deep-link. The doc's markdown rides IN the URL as the `c=`
# param — gzip → base64url (no padding) — which the live md-converter decodes on
# mount (src/lib/inline). One source: no md-converter fork, no upload, no fetch.
# Contract is byte-identical to its TS encoder; mtime=0 keeps the URL deterministic.
MDC_BASE = "https://md-converter.designs-os.com"


def mdc_url(markdown: str) -> str:
    packed = base64.urlsafe_b64encode(
        gzip.compress((markdown or "").encode(), mtime=0)).rstrip(b"=").decode()
    return f"{MDC_BASE}/?c={packed}"


def log_event(op: str, *, ok: bool, detail, **fields) -> None:
    """Append one end-to-end event to the rolling webapp log, trimmed to the last
    LOG_MAX_EVENTS. `op` names the operation (publish/snapshot/error/…), `detail`
    is the step trace (a list, or a string we split on newlines), and **fields
    carries op-specific keys (pushed, pr_url, path, …). Best-effort: a logging
    failure must NEVER break the request it records — the log is for visibility,
    not correctness, so any I/O error is swallowed."""
    if isinstance(detail, str):
        detail = detail.splitlines()
    event = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
             "op": op, "ok": ok, **fields, "detail": detail}
    line = json.dumps(event, ensure_ascii=False)
    with _LOG_LOCK:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            prev = LOG_PATH.read_text().splitlines() if LOG_PATH.exists() else []
            prev.append(line)
            LOG_PATH.write_text("\n".join(prev[-LOG_MAX_EVENTS:]) + "\n")
        except OSError:
            pass


def read_log() -> list[dict]:
    """The rolling log as a list of event dicts, oldest→newest. Tolerates a
    partially-written or corrupt line rather than failing the whole read."""
    out: list[dict] = []
    try:
        text = LOG_PATH.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"op": "?", "ok": False, "detail": [line]})
    return out


# Shell fields the review layer may write. seed/L&S/system_prompt/mandate are
# deliberately ABSENT — the law says the shell curates them, so there is no door.
SHELL_EDITABLE = {"current_state"}  # workspace + connections both retired (B5) → current_state is the one writable surface; "where things live" is the derived dr_* map
FLAG_EDITABLE = {"resolved", "resolution_notes", "description", "feature_id", "priority"}
ROADMAP_EDITABLE = {"title", "roadmap_status", "summary", "sort_order", "project_id"}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    # The server and a harness session now write this DB from separate processes
    # (both share the bind-mounted file in the sandbox). WAL lets a reader and a
    # writer coexist; busy_timeout makes a contended write wait instead of raising
    # "database is locked". WAL is a persistent DB property — set once, sticks.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ── Data assembly ─────────────────────────────────────────────────────────────

def get_shells(con) -> list[dict]:
    return rows(con.execute(
        "SELECT shell_id, display_name, shortname, role, flavor, mandate, is_shared "
        "FROM shells WHERE COALESCE(is_deleted,0)=0 ORDER BY shell_id"))


def get_shell(con, sid: int) -> dict | None:
    r = con.execute(
        "SELECT shell_id, display_name, shortname, partner, role, mandate, "
        "system_prompt, current_state, lineage_seed, "
        "has_identity, active_archive_id FROM shells "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0", (sid,)).fetchone()
    if r is None:
        return None
    shell = dict(r)
    shell["seed"] = rows(con.execute(
        "SELECT entry_id, entry_date, body FROM shell_identity_entries "
        "WHERE shell_id=? AND kind='seed' AND is_deleted=0 AND retired_at IS NULL "
        "ORDER BY entry_date, entry_id", (sid,)))
    shell["lns"] = rows(con.execute(
        "SELECT entry_id, entry_date, body FROM shell_identity_entries "
        "WHERE shell_id=? AND kind='lns' AND is_deleted=0 AND retired_at IS NULL "
        "ORDER BY entry_date, entry_id", (sid,)))
    shell["skills"] = rows(con.execute(
        "SELECT s.skill_id, s.name, s.description, s.category, "
        "(SELECT 1 FROM shell_skills ss WHERE ss.shell_id=? AND ss.skill_id=s.skill_id) "
        "AS granted FROM skills s WHERE s.is_deleted=0 ORDER BY s.name", (sid,)))
    tag_origin(shell["skills"])
    shell["decisions"] = rows(con.execute(
        "SELECT decision_id, decision_date, priority, decision FROM shell_decisions "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0 ORDER BY decision_id DESC "
        "LIMIT 25", (sid,)))
    return shell


def tag_origin(skills: list[dict]) -> list[dict]:
    """Annotate skill rows with origin: 'engine' | 'repo'.

    Same rule snapshot.py uses to decide what serializes into content.sql —
    a name under assets/skills/ is engine catalogue; anything else is a
    repo-local skill. One rule, two consumers: the UI's "Repo skills" section
    shows exactly what the snapshot will keep durable."""
    engine = set(snapshot_mod.engine_skill_names())
    for s in skills:
        s["origin"] = "engine" if s["name"] in engine else "repo"
    return skills


def get_skills(con) -> dict:
    """The full skills catalogue + per-skill grants, for the Skills tab.
    Grouping into sections (repo / category) happens client-side, like
    flags/docs."""
    skills = rows(con.execute(
        "SELECT skill_id, name, description, category, command, common "
        "FROM skills WHERE is_deleted=0 ORDER BY name"))
    tag_origin(skills)
    grants: dict[int, list] = {}
    for g in rows(con.execute(
            "SELECT ss.skill_id, ss.shell_id FROM shell_skills ss "
            "JOIN shells sh ON sh.shell_id=ss.shell_id "
            "WHERE COALESCE(sh.is_deleted,0)=0 ORDER BY ss.shell_id")):
        grants.setdefault(g["skill_id"], []).append(g["shell_id"])
    for s in skills:
        s["granted_shells"] = grants.get(s["skill_id"], [])
    return {"skills": skills, "shells": get_shells(con)}


# Board order: delivered work first, then the committed funnel read backward
# (most-active → farthest-out) — items move LEFT toward shipped as long_term
# matures to near_term, next, in_progress, shipped. brainstorm (idea inlet) and
# retired (taken off the board) are the right-hand end caps.
_ORDER = ["in_progress", "next", "near_term", "long_term", "brainstorm", "retired", "shipped"]
_LABEL = {"brainstorm": "Brainstorm", "in_progress": "In Progress", "next": "Next",
          "near_term": "Near Term", "long_term": "Long Term", "shipped": "Shipped",
          "retired": "Retired"}


def get_roadmap(con) -> dict:
    feats = rows(con.execute(
        "SELECT r.feature_id, r.title, r.roadmap_status, r.sort_order, r.summary, "
        "r.project_id, p.title AS project_title, "
        "s.shortname AS owner FROM roadmap r "
        "LEFT JOIN shells s ON s.shell_id=r.owning_shell "
        "LEFT JOIN projects p ON p.project_id=r.project_id "
        "ORDER BY r.sort_order, r.feature_id"))
    # Roadmap tracks the development cycle = the SPECS, with each feature's DOCS
    # (kind='doc') listed underneath so specs and docs sit together. Docs are
    # read-only here (open-link only); the Docs tab is where they're edited.
    # kind DESC orders 'spec' before 'doc' within a feature.
    docs_by: dict[int, list] = {}
    for d in rows(con.execute(
            "SELECT document_id, feature_id, kind, seq, title, frozen, frozen_date, "
            "render_path FROM documents WHERE kind IN ('spec','doc') "
            "ORDER BY feature_id, kind DESC, seq")):
        docs_by.setdefault(d["feature_id"], []).append(d)
    flags_by: dict[int, list] = {}
    for f in rows(con.execute(
            "SELECT flag_id, feature_id, display_name, description FROM flags "
            "WHERE resolved=0 AND COALESCE(is_deleted,0)=0 AND feature_id IS NOT NULL")):
        flags_by.setdefault(f["feature_id"], []).append(f)
    # Spec tasks (implementation plan) attach per feature, ordered by spec then
    # seq so a multi-spec feature lists each spec's plan in order. Drives the
    # feature card's task checklist + side-bar colour in the UI.
    tasks_by: dict[int, list] = {}
    for t in rows(con.execute(
            "SELECT task_id, feature_id, document_id, seq, title, status "
            "FROM spec_tasks ORDER BY feature_id, document_id, seq")):
        tasks_by.setdefault(t["feature_id"], []).append(t)
    # Blocking edges: feature_id is blocked by each blocked_by. The Flow view
    # draws these as arrows; the feature card's "blocked by" editor sets them.
    blockers_by: dict[int, list] = {}
    for e in rows(con.execute(
            "SELECT feature_id, blocked_by FROM feature_blockers")):
        blockers_by.setdefault(e["feature_id"], []).append(e["blocked_by"])
    for f in feats:
        f["documents"] = docs_by.get(f["feature_id"], [])
        f["open_flags"] = flags_by.get(f["feature_id"], [])
        f["tasks"] = tasks_by.get(f["feature_id"], [])
        f["blockers"] = blockers_by.get(f["feature_id"], [])
    buckets = [{"status": s, "label": _LABEL[s],
                "features": [f for f in feats if f["roadmap_status"] == s]}
               for s in _ORDER]
    # Active work-streams, for the Board's per-project grouping + the feature
    # card's project picker. Each feature already carries project_id/project_title.
    projects = rows(con.execute(
        "SELECT project_id, shortname, title FROM projects "
        "WHERE COALESCE(is_deleted,0)=0 AND status='active' ORDER BY title"))
    return {"buckets": [b for b in buckets if b["features"]], "projects": projects}


def get_docs(con) -> dict:
    """Documentation (kind='doc'), grouped client-side by feature. Distinct from
    the spec dev-cycle the roadmap tracks."""
    return {"docs": rows(con.execute(
        "SELECT d.document_id, d.feature_id, d.kind, d.seq, d.title, d.frozen, "
        "d.frozen_date, r.title AS feature_title FROM documents d "
        "LEFT JOIN roadmap r ON r.feature_id = d.feature_id "
        "WHERE d.kind='doc' ORDER BY d.feature_id, d.seq"))}


_EMPTY_MAP = {"repo": None, "total_files": 0, "by_lang": [],
              "by_role": [], "deps": [], "env": []}


def get_map() -> dict:
    """The dr_* repo catalogue, summarized — how the shell (and the FnB) sees
    what's in the host repo. The catalogue lives in its OWN db (.sc-state/map.db),
    separate from shell_db.db, so read it read-only there; degrade to an empty
    'not mapped yet' shape when the fork hasn't been mapped."""
    con = map_db.open_ro()
    if con is None:
        return dict(_EMPTY_MAP)
    try:
        repo = con.execute("SELECT * FROM dr_repo WHERE repo_id=1").fetchone()
        total = con.execute("SELECT COUNT(*) FROM dr_filepath").fetchone()[0]
        return {
            "repo": dict(repo) if repo else None,
            "total_files": total,
            "by_lang": rows(con.execute(
                "SELECT lang, COUNT(*) AS n, COALESCE(SUM(lines),0) AS lines "
                "FROM dr_filepath WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC")),
            "by_role": rows(con.execute(
                "SELECT role, COUNT(*) AS n FROM dr_filepath GROUP BY role ORDER BY n DESC")),
            "deps": rows(con.execute(
                "SELECT manager, name, version, kind, source_file FROM dr_dependency "
                "ORDER BY manager, name")),
            "env": rows(con.execute(
                "SELECT name, source_file FROM dr_env ORDER BY name")),
        }
    finally:
        con.close()


def get_flags(con) -> dict:
    flags = rows(con.execute(
        "SELECT f.flag_id, f.display_name, f.priority, f.description, f.created_date, "
        "f.resolved, f.resolved_date, f.resolution_notes, f.feature_id, "
        "r.title AS feature_title FROM flags f LEFT JOIN roadmap r "
        "ON r.feature_id=f.feature_id WHERE COALESCE(f.is_deleted,0)=0 "
        "ORDER BY f.resolved, f.flag_id DESC"))
    features = rows(con.execute(
        "SELECT feature_id, title FROM roadmap ORDER BY sort_order, feature_id"))
    return {"flags": flags, "features": features}


# ── Mutations ─────────────────────────────────────────────────────────────────

def patch_columns(con, table, pk_col, pk, body, allowed):
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return False, "no editable fields in payload"
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE {table} SET {sets} WHERE {pk_col}=?",
                (*fields.values(), pk))
    con.commit()
    return True, None


def _reaches_via_blockers(adj, start, target) -> bool:
    """Can `target` be reached from `start` by following blocked_by edges? Used
    to keep the blocker graph acyclic: if a candidate blocker already depends
    (transitively) on the feature, adding the edge would close a cycle."""
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        if n == target:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, ()))
    return False


def set_blockers(con, feature_id, blocked_by):
    """Replace feature_id's entire blocker set (idempotent). Validates that every
    id exists, none is the feature itself, and no edge closes a cycle (app-level,
    since SQLite can't express it). Returns (ok, error)."""
    if con.execute("SELECT 1 FROM roadmap WHERE feature_id=?",
                   (feature_id,)).fetchone() is None:
        return False, "no such feature"
    try:
        ids = list(dict.fromkeys(int(b) for b in (blocked_by or [])))
    except (TypeError, ValueError):
        return False, "blocked_by must be a list of feature ids"
    if feature_id in ids:
        return False, "a feature cannot block itself"
    for b in ids:
        if con.execute("SELECT 1 FROM roadmap WHERE feature_id=?",
                       (b,)).fetchone() is None:
            return False, f"no such feature: {b}"
    # Cycle guard: rebuild adjacency WITHOUT feature_id's own edges (they're being
    # replaced), then reject any new blocker that can already reach feature_id.
    adj: dict[int, list] = {}
    for e in rows(con.execute(
            "SELECT feature_id, blocked_by FROM feature_blockers "
            "WHERE feature_id<>?", (feature_id,))):
        adj.setdefault(e["feature_id"], []).append(e["blocked_by"])
    for b in ids:
        if _reaches_via_blockers(adj, b, feature_id):
            return False, (f"that would create a cycle — feature {b} already "
                           f"depends on feature {feature_id}")
    con.execute("DELETE FROM feature_blockers WHERE feature_id=?", (feature_id,))
    con.executemany(
        "INSERT INTO feature_blockers (feature_id, blocked_by) VALUES (?, ?)",
        [(feature_id, b) for b in ids])
    con.commit()
    return True, None


def patch_document(con, doc_id, body):
    r = con.execute("SELECT frozen FROM documents WHERE document_id=?",
                    (doc_id,)).fetchone()
    if r is None:
        return False, "no such document"
    if r["frozen"]:
        return False, "document is frozen — open the next spec, don't edit this one"
    return patch_columns(con, "documents", "document_id", doc_id, body,
                          {"body", "title"})


def create_flag(con, body):
    if not body.get("description"):
        return None, "description required"
    cur = con.execute(
        "INSERT INTO flags (display_name, description, priority, feature_id, shell_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (body.get("display_name"), body["description"],
         body.get("priority", "Medium"), body.get("feature_id"),
         body.get("shell_id")))
    con.commit()
    return cur.lastrowid, None


def _slug(text: str) -> str:
    """title → kebab shortname: keep alnum, fold spaces/_-/ to single dashes."""
    out = []
    for ch in (text or "").lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "project"


def create_project(con, body):
    """Create a work-stream (projects row) from a title. shortname is slugified
    from the title, de-duped with a numeric suffix. Used by the roadmap Board's
    inline '＋ new work-stream' so features can be grouped without leaving the UI."""
    title = (body.get("title") or "").strip()
    if not title:
        return None, "title required"
    base = _slug(title)
    shortname, n = base, 2
    while con.execute("SELECT 1 FROM projects WHERE shortname=?",
                      (shortname,)).fetchone():
        shortname, n = f"{base}-{n}", n + 1
    cur = con.execute("INSERT INTO projects (shortname, title) VALUES (?, ?)",
                      (shortname, title))
    con.commit()
    return {"project_id": cur.lastrowid, "shortname": shortname, "title": title}, None


def set_grant(con, sid, skill_id, granted):
    if granted:
        con.execute("INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
                    "VALUES (?, ?)", (sid, skill_id))
    else:
        con.execute("DELETE FROM shell_skills WHERE shell_id=? AND skill_id=?",
                    (sid, skill_id))
    con.commit()


# Whitelisted maintenance scripts runnable from the GUI. Each is a fixed argv —
# the GUI passes only a registry KEY, never a command, so nothing arbitrary runs.
# Order = display order; `danger` ones prompt for confirmation in the UI.
_PY = sys.executable
_SCRIPTS = {
    "snapshot": ("Snapshot", "Serialize the per-instance tables → .sc-state/content.sql "
                 "(deterministic, idempotent). Run after editing identity, roadmap, "
                 "docs, or flags so the change survives a rebuild.",
                 [_PY, str(ENGINE / "scripts/snapshot.py")], False),
    "render": ("Render flat", "Regenerate the tracked flat _sc files "
               "(specs_sc / docs_sc / skills_sc / roadmap_sc.md) from the DB. Incremental.",
               [_PY, str(ENGINE / "scripts/render.py"), "flat"], False),
    "seed_skills": ("Seed skills", "Recompile assets/skills/ into the skills seed migration "
                    "(migrations/0001_seed_skills.sql). Run after editing a skill body.",
                    [_PY, str(ENGINE / "scripts/seed_skills.py")], False),
    "migrate": ("Migrate", "Apply any pending migrations to the live DB (ledger-tracked).",
                [_PY, str(ENGINE / "scripts/migrate.py"), str(DB_PATH)], False),
    "map": ("Map repo", "Scan the host repo into the dr_* catalogue "
            "(files / deps / env) — how the shell reads its repo. Re-run when the "
            "repo changes.", [_PY, str(ENGINE / "scripts/map_repo.py")], False),
    "rebuild": ("Rebuild DB", "Rebuild shell_db.db from schema + migrations + snapshot "
                "(backs up the current DB first). Discards any DB edits you have NOT "
                "snapshotted.", [_PY, str(ENGINE / "scripts/rebuild.py")], True),
}


def script_list() -> list[dict]:
    return [{"key": k, "name": v[0], "desc": v[1], "danger": v[3]}
            for k, v in _SCRIPTS.items()]


def run_script(key: str) -> dict | None:
    spec = _SCRIPTS.get(key)
    if not spec:
        return None
    argv = spec[2]
    try:
        # The API is the admin/GUI surface — snapshot/render here are sanctioned,
        # so pass SC_ADMIN to clear the serialize guard (see _serialize_guard.py).
        p = subprocess.run(argv, capture_output=True, text=True,
                           cwd=str(REPO_ROOT), timeout=180,
                           env={**os.environ, "SC_ADMIN": "1"})
        return {"ok": p.returncode == 0, "code": p.returncode,
                "output": (p.stdout + p.stderr).strip() or "(no output)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": "timed out (>180s)"}


def run_snapshot_render() -> str:
    """The header 'snapshot ⤓' shortcut — serialize then render. Raises on either
    step's failure so publish can never commit/push stale flat files over a DB it
    failed to serialize (the old code ignored returncode and returned anyway)."""
    snap = run_script("snapshot")
    if not snap["ok"]:
        raise RuntimeError("snapshot failed:\n" + snap["output"])
    rend = run_script("render")
    if not rend["ok"]:
        raise RuntimeError("render failed:\n" + rend["output"])
    return (snap["output"] + "\n" + rend["output"]).strip()


# ── Publish: serialize → render → commit → push → open/update one PR ──────────
# Ephemeral-branch model: each publish (re)creates the local branch from HEAD,
# commits the serialized content + renders onto it, force-pushes, opens/updates
# ONE PR to main — then returns to main and DELETES the local branch. No merge:
# the open PR is the gate (the FnB merges on GitHub). The branch NAME is stable
# (one rolling PR) but the local branch is EPHEMERAL — rebuilt + dropped every
# publish — so the working tree is always left clean on main and branches never
# accumulate. Push + PR need a GitHub token (SC_GH_TOKEN / GH_TOKEN); `./sc
# launch` forwards it into the sandbox. Without a token the change is still
# COMMITTED locally (the unpushed branch is kept so the commit isn't lost) — only
# push/PR is skipped, with a clear message. A module lock serializes concurrent
# publishes (one git index).
BASE_BRANCH = "main"
PUBLISH_BRANCH = "sc_gui_content"
_PUBLISH_LOCK = threading.Lock()
# The git-tracked text the DB rebuilds from + the flat renders. NOT the .db
# (gitignored). schema.sql + migrations are engine paths: TRACKED in the source
# repo, GITIGNORED in a fork (B7) — git_publish() filters ignored paths so the
# same list self-adapts (they stay in source, drop out in a fork, where the
# engine is a materialized dependency authored upstream). .sc-state/ is the
# fork-owned memory serialization + engine pin (always tracked).
PUBLISH_PATHS = [
    ".sc-state/content.sql",
    ".sc-state/engine.ref",
    ".super-coder/schema.sql",
    ".super-coder/migrations",
    "specs_sc", "docs_sc", "skills_sc", "roadmap_sc.md",
]
# Everything publish touches is REGENERATED from the live DB by snapshot+render —
# so a working-tree change to any of these paths is disposable: the next snapshot
# rewrites it identically from the source of truth. That is the lever that lets
# publish move branches safely even from a dirty/stranded tree. content.sql's
# sibling map_content.sql is regenerated by the same snapshot but isn't published.
REGENERABLE_PATHS = PUBLISH_PATHS + [".sc-state/map_content.sql"]


def _git(*args):
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)


def _porcelain_paths(*pathspec) -> list[str]:
    """Tracked working-tree changes (optionally limited to pathspec), as repo-rel
    paths. Rename lines ('R  old -> new') yield the new path; untracked excluded."""
    r = _git("status", "--porcelain", "--untracked-files=no", "--", *pathspec)
    paths = []
    for line in r.stdout.splitlines():
        p = line[3:]
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip().strip('"'))
    return paths


def _restore_regenerable(out: list) -> None:
    """Discard working-tree edits limited to the regenerable set so a branch
    switch can't fail on a dirty tree. snapshot rewrites them from the DB next."""
    # Restore only the concrete dirty paths, not the whole REGENERABLE_PATHS list:
    # `git checkout -- <pathspec>` is fatal on ANY non-matching pathspec (a fork
    # lacks some of these), which would abort the restore and reset nothing.
    dirty = _porcelain_paths(*REGENERABLE_PATHS)
    if dirty:
        _git("checkout", "--", *dirty)
        out.append(f"(reset {len(dirty)} regenerable file(s))")


def _unexpected_dirty() -> list[str]:
    """Dirty TRACKED files outside the regenerable set — real user work publish
    must never clobber. Used to refuse rather than reset an unexpected tree."""
    def _regen(p: str) -> bool:
        return any(p == r or p.startswith(r.rstrip("/") + "/")
                   for r in REGENERABLE_PATHS)
    return [p for p in _porcelain_paths() if not _regen(p)]


def _gh_token() -> str:
    env = (os.environ.get("SC_GH_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if env:
        return env
    # Host-run server (started directly, not via `./sc launch` which forwards
    # GH_TOKEN into the sandbox): fall back to the host's gh login so a web-authed
    # `gh auth login` "just works" with no token to export. Mirrors what `sc`
    # itself does. In the sandbox gh usually isn't installed, so this fails to ""
    # cleanly and the token simply comes from the forwarded env above.
    try:
        r = subprocess.run(["gh", "auth", "token"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _origin_https() -> str | None:
    """origin URL as https (ssh `git@host:owner/repo` → https), so a token push
    needs no ssh keys in the container."""
    url = _git("remote", "get-url", "origin").stdout.strip()
    if not url:
        return None
    if url.startswith("git@"):
        url = "https://" + url.split("@", 1)[1].replace(":", "/", 1)
    if url.startswith("https://") and not url.endswith(".git"):
        url += ".git"
    return url


def _redact(s: str, token: str) -> str:
    return s.replace(token, "***") if token else s


def git_publish() -> dict:
    out: list[str] = []
    # state survives into the finally so cleanup knows whether the commit reached
    # origin (safe to drop the local branch) or only exists locally (keep it).
    state: dict = {"ok": True, "pr_url": None, "pushed": False}
    try:
        # 1. Get onto a clean BASE and (re)create the ephemeral branch BEFORE any
        #    snapshot writes. The old order serialized first, which dirtied
        #    whatever branch the tree happened to be on; if a prior run had left it
        #    stranded on the publish branch, the next publish could then neither
        #    delete that branch (it was current) nor check out main (the dirty,
        #    regenerated content blocked it) — a self-perpetuating stuck state.
        #    Preparing the branch first means the serialize lands on the publish
        #    branch and can never block its own creation.
        if _prepare_branch(out, state):
            # 2. serialize the DB → git-tracked text + render the flat files.
            out.append(run_snapshot_render())
            # 3. stage → commit → push → open/update one PR.
            _publish_content(out, state)
    except Exception as e:
        state["ok"] = False
        out.append(f"✗ publish error: {e}")
    finally:
        # Always land back on main and drop the ephemeral local branch — runs even
        # if a step raised or returned early, so the tree never stays stranded on
        # the publish branch.
        _land_on_base(out, state)
    # Record the full end-to-end trace (success OR failure) so a publish that
    # "looked done" can be inspected after the fact — the gap that made the live
    # incident unexplainable. _land_on_base above appends to `out`, so log here.
    log_event("publish", ok=state["ok"], pushed=state["pushed"],
              pr_url=state["pr_url"], detail=out)
    return {"ok": state["ok"], "output": "\n".join(out), "pr_url": state["pr_url"]}


def _prepare_branch(out: list, state: dict) -> bool:
    """Land on a clean BASE_BRANCH and (re)create the ephemeral publish branch.
    Returns True only when the tree is sitting on a fresh PUBLISH_BRANCH ready for
    the snapshot. Recovers automatically from a tree stranded on a stale publish
    branch, but refuses (rather than clobbers) if unrelated user work is dirty."""
    # Refuse to touch a tree carrying real, non-regenerable changes — that's user
    # work, not publishable content, and branch moves below would disrupt it.
    unexpected = _unexpected_dirty()
    if unexpected:
        state["ok"] = False
        out.append("✗ working tree has non-content changes — refusing to publish "
                   "(commit or stash them first):\n"
                   + "\n".join(f"  {p}" for p in unexpected[:20])
                   + ("\n  …" if len(unexpected) > 20 else ""))
        return False

    # Discard regenerable dirt so the checkout below can't fail on a dirty tree.
    _restore_regenerable(out)

    # Land on base. A prior crash/early-return can leave us on the publish branch.
    cur = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != BASE_BRANCH:
        co = _git("checkout", BASE_BRANCH)
        if co.returncode != 0:
            state["ok"] = False
            out.append(f"✗ can't switch to {BASE_BRANCH} from {cur}:\n{co.stderr.strip()}")
            return False
        if cur == PUBLISH_BRANCH:
            out.append(f"(recovered onto {BASE_BRANCH} from stranded {PUBLISH_BRANCH})")
        else:
            out.append(f"(switched to {BASE_BRANCH} from {cur})")

    # Drop a stale local publish branch — now safe because it isn't the current
    # branch. The old code ran `branch -D` while still ON it: the delete failed,
    # its returncode was ignored, and it falsely logged "(dropped stale …)".
    if _git("rev-parse", "--verify", "--quiet",
            f"refs/heads/{PUBLISH_BRANCH}").returncode == 0:
        has_origin = _git("rev-parse", "--verify", "--quiet",
                          f"refs/remotes/origin/{PUBLISH_BRANCH}").returncode == 0
        rng = [f"{BASE_BRANCH}..{PUBLISH_BRANCH}"]
        if has_origin:
            rng.append(f"^origin/{PUBLISH_BRANCH}")
        unmerged = _git("log", "--oneline", *rng).stdout.strip()
        bd = _git("branch", "-D", PUBLISH_BRANCH)
        if bd.returncode != 0:
            state["ok"] = False
            out.append(f"✗ can't drop stale local {PUBLISH_BRANCH}:\n{bd.stderr.strip()}")
            return False
        if unmerged:
            n = len(unmerged.splitlines())
            out.append(f"(dropped stale local {PUBLISH_BRANCH}; it had {n} commit(s) "
                       "not on base or origin — publish regenerates content from the DB)")
        else:
            out.append(f"(dropped stale local {PUBLISH_BRANCH})")

    sw = _git("checkout", "-b", PUBLISH_BRANCH)
    if sw.returncode != 0:
        state["ok"] = False
        out.append(f"✗ can't create '{PUBLISH_BRANCH}':\n{sw.stderr.strip()}")
        return False
    out.append(f"on ephemeral branch {PUBLISH_BRANCH}")
    return True


def _publish_content(out: list, state: dict) -> None:
    # The ephemeral branch is already created clean from base by _prepare_branch,
    # and the snapshot+render has written the publishable content onto it.
    # 3. stage the publishable text + renders; commit if anything changed.
    #    Filter to paths that exist — `git add` is fatal on a missing pathspec, and
    #    a minimal fork may lack some (e.g. docs_sc/ before any doc is authored).
    #    Drop gitignored paths too: in a fork the engine (schema/migrations) is
    #    ignored, and `git add -- <ignored>` aborts the WHOLE add (staging
    #    nothing). check-ignore lets the same list serve source + fork.
    def _ignored(p: str) -> bool:
        return _git("check-ignore", "-q", "--", p).returncode == 0
    present = [p for p in PUBLISH_PATHS
              if (REPO_ROOT / p).exists() and not _ignored(p)]
    if present:
        _git("add", "--", *present)
    staged = _git("diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        out.append(f"✓ no content changes vs {BASE_BRANCH} — nothing to publish")
        return
    n = len(staged.splitlines())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (f"gui: publish content edits ({n} file{'s' if n != 1 else ''})\n\n"
           f"Serialized + rendered from the review GUI at {stamp}.\n\n"
           + "\n".join(f"- {f}" for f in staged.splitlines()))
    c = _git("commit", "-m", msg)
    if c.returncode != 0:
        state["ok"] = False
        out.append("✗ commit failed:\n" + (c.stderr or c.stdout).strip())
        return
    out.append(f"committed {n} file(s)")

    # 4. token gate: committed locally either way, but push/PR needs a token.
    token = _gh_token()
    if not token:
        out.append("⚠ committed locally, but no GH_TOKEN — can't push or open a "
                   "PR. Set SC_GH_TOKEN, or `./sc launch` with a host gh login.")
        return

    # 5. force-push: the branch is recreated from HEAD each publish (one commit
    #    ahead of main — the full current state), so it intentionally overwrites
    #    the prior rolling head. Only publish ever writes this branch, so --force
    #    is safe and force-with-lease's tracking-ref dance is unnecessary.
    url = _origin_https()
    if not url:
        state["ok"] = False
        out.append("✗ no 'origin' remote to push to.")
        return
    push_url = url.replace("https://", f"https://x-access-token:{token}@", 1)
    p = _git("push", "--force", push_url, f"{PUBLISH_BRANCH}:{PUBLISH_BRANCH}")
    if p.returncode != 0:
        state["ok"] = False
        out.append("✗ push failed:\n" + _redact((p.stderr or p.stdout).strip(), token))
        return
    state["pushed"] = True
    out.append(f"force-pushed {PUBLISH_BRANCH} → origin")

    # 6. upsert ONE PR — no merge; the open PR is the gate the FnB merges.
    env = {**os.environ, "GH_TOKEN": token}

    def gh(*args):
        return subprocess.run(["gh", *args], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, env=env)

    pr_url = gh("pr", "view", PUBLISH_BRANCH, "--json", "url", "-q", ".url").stdout.strip()
    if not pr_url:
        cr = gh("pr", "create", "--base", BASE_BRANCH, "--head", PUBLISH_BRANCH,
                "--title", "GUI content edits",
                "--body", "Rolling PR for content edited via the super-coder "
                "review GUI (roadmap, docs, flags, identity). Refreshed on each "
                "publish; merge to land on main.")
        if cr.returncode != 0:
            state["ok"] = False
            out.append("✗ PR create failed:\n" + _redact((cr.stderr or cr.stdout).strip(), token))
            return
        pr_url = cr.stdout.strip()
        out.append(f"opened PR: {pr_url}")
    else:
        out.append(f"updated PR: {pr_url}")
    state["pr_url"] = pr_url


def _land_on_base(out: list, state: dict) -> None:
    """Return to main and drop the ephemeral local branch — the pushed remote
    branch + its PR are what persist. If the commit was NOT pushed (no token /
    push failed), KEEP the local branch so its commit isn't lost; the live DB is
    still the source of truth and a later `snapshot` regenerates the same text."""
    now = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if now != BASE_BRANCH:
        # If a step raised after the snapshot but before the commit, the publish
        # branch is left dirty with regenerated content — discard it (the DB is
        # the source of truth) so the checkout can't fail and re-strand the tree.
        _restore_regenerable(out)
        co = _git("checkout", BASE_BRANCH)
        if co.returncode != 0:
            out.append(f"⚠ left on {now} — couldn't return to {BASE_BRANCH}:\n"
                       f"{co.stderr.strip()}")
            return
    local_exists = _git("rev-parse", "--verify", "--quiet",
                        f"refs/heads/{PUBLISH_BRANCH}").returncode == 0
    if local_exists and state["pushed"]:
        _git("branch", "-D", PUBLISH_BRANCH)
        out.append(f"↩ back on {BASE_BRANCH}; local {PUBLISH_BRANCH} cleaned up")
    elif local_exists:
        out.append(f"↩ back on {BASE_BRANCH}; kept local {PUBLISH_BRANCH} "
                   "(unpushed commit preserved)")
    else:
        out.append(f"↩ back on {BASE_BRANCH}")


# ── HTTP ──────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "super-coder/1.0"

    def _send(self, code, payload, ctype="application/json"):
        body = (json.dumps(payload) if ctype.startswith("application/json")
                else payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, *a):  # quiet
        pass

    def _fail(self, exc: Exception):
        """Unhandled handler error. The do_* methods swallow everything so one
        bad request can't kill the server thread — but a silent `400 {error:
        str(exc)}` hid genuine SERVER bugs behind a client-error status with no
        trace (a SELECT omitting a column read by key surfaced only as
        `{"error": "'feature_id'"}`, no stack, status 400). Log the full
        traceback to stderr and return 500 so it reads as a server fault."""
        traceback.print_exc()
        # Also land it in the rolling log so a failed request is visible after the
        # fact, not only in stderr that may have scrolled away / not been captured.
        log_event("error", ok=False, path=getattr(self, "path", "?"),
                  detail=traceback.format_exc().strip().splitlines()[-15:])
        return self._send(500, {"error": str(exc)})

    # -- static + GET --
    def do_GET(self):
        path = urlparse(self.path).path
        if path in _STATIC:
            fname, ctype = _STATIC[path]
            f = UI_DIR / fname
            if not f.exists():
                return self._send(404, "not built", "text/plain")
            return self._send(200, f.read_text(), ctype)
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        # git-hygiene is a live filesystem/git read — no DB, computed on demand
        # (the UI refresh button is the only trigger). `?fetch=1` does the network
        # fetch for accurate behind-counts + fresh PR state; the default skips it
        # so the initial tab load is snappy.
        if urlparse(self.path).path == "/api/git-state":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            fetch = q.get("fetch", ["0"])[0] in ("1", "true", "yes")
            try:
                return self._send(200, git_hygiene.compute(fetch=fetch))
            except Exception as e:
                return self._fail(e)
        # Rolling webapp event log — no DB, just the last LOG_MAX_EVENTS events.
        # Newest-first for the reader; reachable from the browser/curl so you don't
        # have to shell into the sandbox to see what a publish/snapshot did.
        if path == "/api/logs":
            return self._send(200, {"events": list(reversed(read_log())),
                                    "max": LOG_MAX_EVENTS})
        con = db()
        try:
            if path == "/api/health":
                cfg = ports_mod.resolve()
                return self._send(200, {"ok": True, "repo": cfg.get("repo"),
                                        "port": cfg.get("port")})
            if path == "/api/shells":
                return self._send(200, {"shells": get_shells(con)})
            if path == "/api/shell-templates":
                return self._send(200, {"templates": shell_factory.flavors()})
            if path.startswith("/api/shells/"):
                sid = int(path.rsplit("/", 1)[1])
                shell = get_shell(con, sid)
                return self._send(200 if shell else 404,
                                  shell or {"error": "no such shell"})
            if path == "/api/skills":
                return self._send(200, get_skills(con))
            if path.startswith("/api/skills/"):
                kid = int(path.rsplit("/", 1)[1])
                r = con.execute(
                    "SELECT skill_id, name, description, category, command, "
                    "common, content FROM skills WHERE skill_id=? AND is_deleted=0",
                    (kid,)).fetchone()
                if r is None:
                    return self._send(404, {"error": "no such skill"})
                return self._send(200, tag_origin([dict(r)])[0])
            if path == "/api/roadmap":
                return self._send(200, get_roadmap(con))
            if path == "/api/docs":
                return self._send(200, get_docs(con))
            if path == "/api/map":
                return self._send(200, get_map())
            if path.startswith("/api/documents/"):
                parts = path.strip("/").split("/")   # api documents {id} [open]
                did = int(parts[2])
                if len(parts) == 4 and parts[3] == "open":
                    r = con.execute("SELECT body FROM documents WHERE document_id=?",
                                    (did,)).fetchone()
                    if r is None:
                        return self._send(404, {"error": "no such document"})
                    return self._redirect(mdc_url(r["body"]))
                r = con.execute("SELECT * FROM documents WHERE document_id=?",
                                (did,)).fetchone()
                return self._send(200 if r else 404,
                                  dict(r) if r else {"error": "no such document"})
            if path == "/api/flags":
                return self._send(200, get_flags(con))
            if path == "/api/scripts":
                return self._send(200, {"scripts": script_list()})
            if path == "/api/vm":
                return self._send(200, {"vm": vm_mod.read()})
            if path == "/api/ts":
                return self._send(200, {"ts": ts_mod.read()})
            if path == "/api/ts/status":
                # Live tailnet view. Needs the host node, so proxy to the
                # ts-broker in the sandbox; call directly on the no-docker host.
                if os.environ.get("SC_SANDBOX"):
                    try:
                        return self._send(200, ts_mod.broker_call("GET", "/status"))
                    except ConnectionError:
                        return self._send(503, {
                            "ok": False,
                            "output": "tailnet status needs the host ts-broker — "
                                      "start it with `./sc ts-broker-up` on the host."})
                return self._send(200, ts_mod.do_status())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def do_POST(self):
        path = urlparse(self.path).path
        con = db()
        try:
            if path == "/api/flags":
                fid, err = create_flag(con, self._body())
                return self._send(400 if err else 201,
                                  {"error": err} if err else {"flag_id": fid})
            if path == "/api/projects":
                proj, err = create_project(con, self._body())
                return self._send(400 if err else 201,
                                  {"error": err} if err else proj)
            if path == "/api/shells":
                body = self._body()
                if not body.get("name") or not body.get("flavor"):
                    return self._send(400, {"error": "name and flavor required"})
                sid = shell_factory.create_shell(
                    con, flavor=body["flavor"], name=body["name"],
                    shortname=body.get("shortname"), partner=body.get("partner"))
                con.commit()
                sn = con.execute(
                    "SELECT shortname FROM shells WHERE shell_id=?", (sid,)).fetchone()[0]
                return self._send(201, {"shell_id": sid, "shortname": sn})
            if path == "/api/snapshot":
                try:
                    out = run_snapshot_render()
                except Exception as e:
                    # run_snapshot_render raises on a failed serialize/render; log
                    # the failure before re-raising so it's in the rolling log too.
                    log_event("snapshot", ok=False, detail=str(e))
                    raise
                log_event("snapshot", ok=True, detail=out)
                return self._send(200, {"output": out})
            if path == "/api/publish":
                with _PUBLISH_LOCK:
                    r = git_publish()
                return self._send(200 if r["ok"] else 500, r)
            if path.startswith("/api/scripts/"):
                r = run_script(path.rsplit("/", 1)[1])
                if r is None:
                    return self._send(404, {"error": "no such script"})
                return self._send(200 if r["ok"] else 500, r)
            if path.startswith("/api/vm/validate/"):
                # Run ONE live check against the candidate config in the body, so
                # the wizard can test-before-save. A failed check is a normal
                # result the UI renders red — 200 with {ok:false}, not an error.
                #
                # The checks run virsh/ssh, which only work on the HOST. In the
                # sandbox we can't reach the VM, so proxy to the host vm-broker
                # over its unix socket; on the no-docker host path, call directly.
                check = path.rsplit("/", 1)[1]
                cfg = self._body().get("vm") or {}
                if os.environ.get("SC_SANDBOX"):
                    try:
                        r = vm_mod.broker_call("POST", f"/validate/{check}", {"vm": cfg})
                    except ConnectionError:
                        return self._send(503, {
                            "ok": False, "check": check,
                            "output": "live checks need the host vm-broker — start it "
                                      "with `./sc vm-broker-up` on the host, then retry."})
                    if r.get("error") == "no such check":
                        return self._send(404, {"error": "no such check"})
                    return self._send(200, r)
                r = vm_mod.validate(check, cfg)
                if r is None:
                    return self._send(404, {"error": "no such check"})
                return self._send(200, r)
            if path.startswith("/api/ts/validate/"):
                # One live tailnet check against the candidate `ts` block. The
                # checks run the tailscale CLI, which only works on the HOST; in
                # the sandbox, proxy to the ts-broker. Mirror of the vm path.
                check = path.rsplit("/", 1)[1]
                cfg = self._body().get("ts") or {}
                if os.environ.get("SC_SANDBOX"):
                    try:
                        r = ts_mod.broker_call("POST", f"/validate/{check}", {"ts": cfg})
                    except ConnectionError:
                        return self._send(503, {
                            "ok": False, "check": check,
                            "output": "live checks need the host ts-broker — start it "
                                      "with `./sc ts-broker-up` on the host, then retry."})
                    if r.get("error") == "no such check":
                        return self._send(404, {"error": "no such check"})
                    return self._send(200, r)
                r = ts_mod.validate(check, cfg)
                if r is None:
                    return self._send(404, {"error": "no such check"})
                return self._send(200, r)
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def do_PATCH(self):
        path = urlparse(self.path).path
        body = self._body()
        con = db()
        try:
            if path.startswith("/api/shells/") and path.count("/") == 3:
                sid = int(path.rsplit("/", 1)[1])
                ok, err = patch_columns(con, "shells", "shell_id", sid, body,
                                        SHELL_EDITABLE)
                return self._send(200 if ok else 400, {"ok": ok, "error": err})
            if path.startswith("/api/flags/"):
                fid = int(path.rsplit("/", 1)[1])
                if body.get("resolved"):
                    from datetime import date
                    body.setdefault("resolved_date", date.today().isoformat())
                ok, err = patch_columns(con, "flags", "flag_id", fid, body,
                                        FLAG_EDITABLE | {"resolved_date"})
                return self._send(200 if ok else 400, {"ok": ok, "error": err})
            if path.startswith("/api/roadmap/"):
                rid = int(path.rsplit("/", 1)[1])
                ok, err = patch_columns(con, "roadmap", "feature_id", rid, body,
                                        ROADMAP_EDITABLE)
                return self._send(200 if ok else 400, {"ok": ok, "error": err})
            if path.startswith("/api/documents/"):
                did = int(path.rsplit("/", 1)[1])
                ok, err = patch_document(con, did, body)
                return self._send(200 if ok else 400, {"ok": ok, "error": err})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def do_PUT(self):
        # grant toggle: PUT /api/shells/{id}/skills/{skill_id}  {granted: bool}
        # vm block:     PUT /api/vm  {vm: {...}}  (persists to instance.json)
        # ts block:     PUT /api/ts  {ts: {...}}  (persists to instance.json)
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        con = db()
        try:
            if len(parts) == 5 and parts[1] == "shells" and parts[3] == "skills":
                set_grant(con, int(parts[2]), int(parts[4]),
                          bool(self._body().get("granted")))
                return self._send(200, {"ok": True})
            if path == "/api/vm":
                vm = self._body().get("vm")
                if vm is not None and not isinstance(vm, dict):
                    return self._send(400, {"error": "vm must be an object"})
                return self._send(200, {"ok": True, "vm": vm_mod.write(vm)})
            if path == "/api/ts":
                tsb = self._body().get("ts")
                if tsb is not None and not isinstance(tsb, dict):
                    return self._send(400, {"error": "ts must be an object"})
                return self._send(200, {"ok": True, "ts": ts_mod.write(tsb)})
            # PUT /api/roadmap/{id}/blockers  {blocked_by: [ids]} — replace the
            # feature's blocker set (empty list clears it).
            if len(parts) == 4 and parts[1] == "roadmap" and parts[3] == "blockers":
                ok, err = set_blockers(con, int(parts[2]),
                                       self._body().get("blocked_by"))
                return self._send(200 if ok else 400, {"ok": ok, "error": err})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()


def main(argv):
    port = None
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if port is None:
        port = ports_mod.resolve().get("port", 8800)
    if not DB_PATH.exists():
        sys.exit(f"server: no DB at {DB_PATH} — run `make rebuild` first.")
    # Bind 127.0.0.1 by default (the host stance: localhost-only, operator owns
    # network controls). In the container set SC_BIND=0.0.0.0 so docker can
    # publish the port — the jail is the `-p 127.0.0.1:PORT:PORT` mapping, which
    # keeps it loopback-only on the host regardless of the in-container bind.
    bind = os.environ.get("SC_BIND", "127.0.0.1")
    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"super-coder review layer → http://127.0.0.1:{port}  (bind {bind}, DB: {DB_PATH.name})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
