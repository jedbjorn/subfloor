#!/usr/bin/env python3
"""super-coder review layer — a localhost server.

One process serves the JSON API, the static review UI, and (sprint 25 seq 5+)
the Interface WebSocket streams on a single per-fork port (see
scripts/ports.py + api/transport.py). The review layer stays zero-dependency
stdlib; the Interface transport pins `websockets` (spec #20: a maintained
stream stack, never hand-rolled framing). When websockets/tmux are absent the
review UI still serves and Interface reports unavailable (spec #20 req 13).
Single-user, localhost — network controls are the operator's, exactly like
superCC's API surface.

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

import asyncio
import base64
import gzip
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
# mem get decisions default index size (#274) — active rows, newest-first.
# A size backstop behind the semantic filter (superseded rows excluded); the
# client footer names what was hidden, so the cap is never silent.
DECISIONS_INDEX_CAP = 30
_LOG_LOCK = threading.Lock()

sys.path.insert(0, str(ENGINE / "scripts"))
import backfill_shell_api_keys  # noqa: E402  (startup key provisioning)
import db_driver  # noqa: E402
import git_hygiene  # noqa: E402  (live repo dirty/stale/clean snapshot)
import interface_reconcile  # noqa: E402  (Interface startup reconciliation)
import interface_wake  # noqa: E402  (transactional wake ingress + coordinator)
import interface_broker  # noqa: E402  (sprint close → binding release, seq 10)
sys.path.insert(0, str(ENGINE / "api"))
try:
    import interface_routes  # noqa: E402  (Interface HTTP API, spec #20)
    import interface_ws  # noqa: E402  (sc-term.v1 stream protocol)
    _INTERFACE_IMPORT_ERROR = None
except ImportError as _exc:  # websockets/tmux stack absent → review UI still serves
    interface_routes = None
    interface_ws = None
    _INTERFACE_IMPORT_ERROR = _exc
import map_db  # noqa: E402  (read-only handle to the dr_* catalogue in map.db)
import pr_poller  # noqa: E402  (watched-PR polling — the service scheduler)
import ports as ports_mod  # noqa: E402
import shell_factory  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402  (engine_skill_names — origin rule)
import model_catalog  # noqa: E402  (live model-id suggestions, sibling module)
import analytics  # noqa: E402  (token & session analytics sweep — doc #11)
import token_parsers  # noqa: E402  (harness roster + per-parser data dirs)
import vm as vm_mod  # noqa: E402  (Windows Test VM — config + live checks)
import ts as ts_mod  # noqa: E402  (tailnet — config + live checks)
import pm2 as pm2_mod  # noqa: E402  (host pm2 stack — config + live checks)

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    # vendored markdown pipeline (marked MIT, DOMPurify MPL-2.0/Apache-2.0) —
    # local copies so the no-build UI renders sanitized GFM without a CDN.
    "/vendor/marked.umd.js": ("vendor/marked.umd.js", "application/javascript; charset=utf-8"),
    "/vendor/purify.min.js": ("vendor/purify.min.js", "application/javascript; charset=utf-8"),
    # vendored browser terminal (xterm.js 6.0.0, MIT — spec #20: a proven
    # terminal-emulation library, never hand-rolled emulation).
    "/vendor/xterm/xterm.js": ("vendor/xterm/xterm.js", "application/javascript; charset=utf-8"),
    "/vendor/xterm/xterm.css": ("vendor/xterm/xterm.css", "text/css; charset=utf-8"),
}

# Content-Security-Policy for the app shell (spec #20 Security And Privacy):
# vendored scripts and same-origin (incl. WebSocket) connections only. Styles
# keep 'unsafe-inline' — the no-build UI sets style attributes via DOM and the
# doc renderer emits inline styling; scripts stay strict.
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'")

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
# display_name is operator-set at creation, so the operator may also correct it.
SHELL_EDITABLE = {"current_state", "display_name"}  # workspace + connections both retired (B5); "where things live" is the derived dr_* map
FLAG_EDITABLE = {"resolved", "resolution_notes", "description", "feature_id", "priority"}
ROADMAP_EDITABLE = {"title", "roadmap_status", "summary", "sort_order", "project_id"}

# Typed traffic on the shell_messages bus (migration 0059). Shells send the
# first three via `sc mem message send --kind`; 'pr_event' is emitted by the
# GitHub watcher daemon (scripts/watch.py), which writes the DB directly —
# accepted here too so a replayed/manual event isn't rejected on kind alone.
MESSAGE_KINDS = {"shell", "task", "result", "pr_event"}
# spec_tasks lifecycle — 'cancelled' (#342) closes a task whose work moved in
# a feature split/re-scope without lying that it was built. Validated here so
# a typo'd status is a 400, not a raw CHECK-constraint 500.
TASK_STATUSES = {"pending", "in_progress", "done", "cancelled"}


def db():
    return db_driver.connect(DB_PATH)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _json_default(o):
    # SQLite hands back bytes for BLOB columns (and for TEXT rows written as
    # bytes by some path); json.dumps can't serialize them and 500s the whole
    # endpoint. Decode UTF-8 with a lossy fallback so one stray bytes value
    # never takes down a read.
    if isinstance(o, (bytes, bytearray)):
        return bytes(o).decode("utf-8", "replace")
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


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
    a name the engine seed (0001) owns is engine catalogue; anything else is a
    repo-local skill (asset-file presence is NOT the rule — a repo skill keeps
    its authoring asset, #253). One rule, two consumers: the UI's "Repo skills"
    section shows exactly what the snapshot will keep durable."""
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


def known_harnesses() -> list[str]:
    """The harness set = the shipped adapters (claude/codex/kimi/opencode/vibe)."""
    d = ENGINE / "adapters"
    if d.exists():
        return sorted(p.name for p in d.iterdir() if p.is_dir())
    return ["claude", "codex", "kimi", "opencode", "vibe"]


def get_flavor_defaults(con) -> dict:
    """The launch-defaults matrix for the Default Models sub-tab: per flavor,
    a model per harness + one starred default harness (flavor_defaults rows —
    the exact table run.py's picker resolves at launch). Template flavors with
    no rows yet are included empty so the GUI matrix is complete; missing
    cells are created on first write (see set_flavor_default)."""
    flavors: dict[str, list] = {}
    for r in rows(con.execute(
            "SELECT flavor, harness, model, is_default FROM flavor_defaults "
            "ORDER BY flavor, harness")):
        flavors.setdefault(r["flavor"], []).append(
            {"harness": r["harness"], "model": r["model"],
             "is_default": bool(r["is_default"])})
    for t in shell_factory.flavors():
        flavors.setdefault(t.get("flavor"), [])
    return {"flavors": flavors, "harnesses": known_harnesses()}


def set_flavor_default(con, body) -> tuple[bool, str | None]:
    """One write to the launch-defaults matrix: set a (flavor, harness) cell's
    model, and/or star the harness as the flavor's default. Starring is
    transactional across the flavor's rows — exactly one is_default=1 after.
    Upserts the row so template flavors / harnesses without a seeded row are
    settable; an empty model clears the cell back to NULL (harness default)."""
    flavor = (body.get("flavor") or "").strip()
    harness = (body.get("harness") or "").strip()
    if not flavor or not harness:
        return False, "flavor and harness required"
    if harness not in known_harnesses():
        return False, f"unknown harness '{harness}'"
    known_flavors = {t.get("flavor") for t in shell_factory.flavors()} | {
        r[0] for r in con.execute("SELECT DISTINCT flavor FROM flavor_defaults")}
    if flavor not in known_flavors:
        return False, f"unknown flavor '{flavor}'"
    if "model" not in body and not body.get("is_default"):
        return False, "nothing to set — pass model and/or is_default"
    con.execute(
        "INSERT INTO flavor_defaults (flavor, harness, model, is_default) "
        "VALUES (?, ?, NULL, 0) ON CONFLICT(flavor, harness) DO NOTHING",
        (flavor, harness))
    if "model" in body:
        model = (body.get("model") or "").strip() or None
        con.execute("UPDATE flavor_defaults SET model=? "
                    "WHERE flavor=? AND harness=?", (model, flavor, harness))
    if body.get("is_default"):
        con.execute("UPDATE flavor_defaults SET is_default = (harness = ?) "
                    "WHERE flavor = ?", (harness, flavor))
    con.commit()
    return True, None


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


# ── Token & session analytics (doc #11) ──────────────────────────────────────
# Read-time views over session_token_usage + the archive lifecycle columns.
# Timestamps are stored UTC; DAY-GROUPING IS THE CLIENT'S JOB (local-time days
# — FnB stance 2026-07-19), so /sessions returns a flat window + cursor, not
# server-grouped days. A "session card" is the usage rows grouped by
# (harness, harness_session_ref) — one harness session, possibly several
# models — enriched with shell/sprint via the attributed archive.

# Every usage row is datable through this (captured_at is always set), so
# windowing can never orphan a row with missing harness timestamps.
_ANALYTICS_TS = "COALESCE(u.started_at, u.ended_at, u.captured_at)"


def _analytics_where(q) -> tuple[str, list]:
    """AND-clause + params from the harness/provider/model/shell query params.
    Column names are hardcoded; values ride as bindings only."""
    conds, params = [], []
    for col in ("harness", "provider", "model"):
        v = (q.get(col, [""])[0] or "").strip()
        if v:
            conds.append(f"u.{col}=?")
            params.append(v)
    shell = (q.get("shell", [""])[0] or "").strip()
    if shell:
        conds.append("u.shell_id=?")
        params.append(int(shell))
    return ("".join(" AND " + c for c in conds)), params


def _card_status(statuses: str, archive_id) -> str:
    """One display status per card: any partial row wins, else no_usage (all
    rows), else ok; unattributed is the archive_id-NULL overlay, not a status."""
    parts = set((statuses or "").split(","))
    if "partial" in parts:
        return "partial"
    if parts == {"no_usage"}:
        return "no_usage"
    return "ok"


def get_analytics_sessions(con, q) -> dict:
    days = max(1, min(int(q.get("days", ["7"])[0]), 183))  # up to the UI's 6-month range chip
    before = (q.get("before", [""])[0] or "").strip() or None
    upper = before or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lower = (datetime.fromisoformat(upper.replace("Z", "+00:00"))
             - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    where, params = _analytics_where(q)
    cards = rows(con.execute(
        f"SELECT u.harness, u.harness_session_ref, "
        f"MIN({_ANALYTICS_TS}) AS started_at, MAX(u.ended_at) AS ended_at, "
        "MAX(u.title) AS title, GROUP_CONCAT(DISTINCT u.model) AS models, "
        "GROUP_CONCAT(DISTINCT u.provider) AS providers, "
        "SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens, "
        "SUM(u.cache_read_tokens) AS cache_read_tokens, "
        "SUM(u.cache_write_tokens) AS cache_write_tokens, "
        "SUM(u.reasoning_tokens) AS reasoning_tokens, "
        "MAX(u.archive_id) AS archive_id, MAX(u.shell_id) AS shell_id, "
        "GROUP_CONCAT(DISTINCT u.status) AS statuses "
        f"FROM session_token_usage u "
        f"WHERE {_ANALYTICS_TS} >= ? AND {_ANALYTICS_TS} < ?{where} "
        "GROUP BY u.harness, u.harness_session_ref "
        "ORDER BY started_at DESC",
        [lower, upper] + params))
    # enrich from the attributed archive + shell in one pass
    aids = [c["archive_id"] for c in cards if c["archive_id"]]
    arch = {}
    if aids:
        marks = ",".join("?" for _ in aids)
        arch = {a["archive_id"]: dict(a) for a in con.execute(
            f"SELECT a.archive_id, a.session_id, a.sprint_ref, s.shortname, "
            f"s.display_name, s.flavor FROM shell_memory_archives a "
            f"JOIN shells s ON s.shell_id=a.shell_id WHERE a.archive_id IN ({marks})",
            aids)}
    for c in cards:
        a = arch.get(c["archive_id"]) or {}
        c["shell"] = a.get("shortname")
        c["shell_session"] = a.get("session_id")
        c["sprint_ref"] = a.get("sprint_ref")
        c["status"] = _card_status(c.pop("statuses"), c["archive_id"])
        c["unattributed"] = c["archive_id"] is None
    older = con.execute(
        f"SELECT 1 FROM session_token_usage u WHERE {_ANALYTICS_TS} < ?{where} LIMIT 1",
        [lower] + params).fetchone()
    return {"sessions": cards, "next_before": lower if older else None}


def get_analytics_tokens(con, q) -> dict:
    where, params = _analytics_where(q)
    bounds, bparams = "", []
    frm = (q.get("from", [""])[0] or "").strip()
    to = (q.get("to", [""])[0] or "").strip()
    if frm:
        bounds += f" AND {_ANALYTICS_TS} >= ?"
        bparams.append(frm)
    if to:
        bounds += f" AND {_ANALYTICS_TS} < ?"
        bparams.append(to)
    sums = ("SUM(u.input_tokens) AS input, SUM(u.output_tokens) AS output, "
            "SUM(u.cache_read_tokens) AS cache_read, "
            "SUM(u.cache_write_tokens) AS cache_write, "
            "SUM(u.reasoning_tokens) AS reasoning")
    totals = dict(con.execute(
        f"SELECT {sums} FROM session_token_usage u WHERE 1=1{bounds}{where}",
        bparams + params).fetchone())
    group_by = (q.get("group_by", [""])[0] or "").strip()
    keys = {"day": f"substr({_ANALYTICS_TS}, 1, 10)",  # UTC day (totals are exact; day buckets are UTC)
            "model": "u.model", "provider": "u.provider", "harness": "u.harness"}
    series = []
    if group_by in keys:
        series = rows(con.execute(
            f"SELECT {keys[group_by]} AS key, {sums} FROM session_token_usage u "
            f"WHERE 1=1{bounds}{where} GROUP BY key ORDER BY key",
            bparams + params))
    return {"totals": totals, "series": series}


def get_analytics_usage(con, q) -> dict:
    """Behavioral reads for the Analytics tab's usage panels. `from`/`to`
    scope the shipped counts to the UI's selected window; comparisons are at
    DAY granularity (substr to the date part) because the source columns mix
    `datetime('now')` (space-separated) and ISO-T formats — full-string
    comparison across the two lies about same-day ordering."""
    frm = (q.get("from", [""])[0] or "")[:10]
    to = (q.get("to", [""])[0] or "")[:10]
    window, wparams = "", []
    if frm:
        window += " AND substr({col}, 1, 10) >= ?"
        wparams.append(frm)
    if to:
        window += " AND substr({col}, 1, 10) <= ?"
        wparams.append(to)

    # favorite model per shell flavor — most sessions wins, read-time only
    fav: dict[str, dict] = {}
    for r in con.execute(
            "SELECT s.flavor, u.model, "
            "COUNT(DISTINCT u.harness || '|' || u.harness_session_ref) AS sessions "
            "FROM session_token_usage u JOIN shells s ON s.shell_id=u.shell_id "
            "WHERE u.model IS NOT NULL AND s.flavor IS NOT NULL "
            "GROUP BY s.flavor, u.model"):
        if r["flavor"] not in fav or r["sessions"] > fav[r["flavor"]]["sessions"]:
            fav[r["flavor"]] = {"flavor": r["flavor"], "model": r["model"],
                                "sessions": r["sessions"]}

    # shipped in the window — updated_at is the read-time proxy for the flip
    # date (the status write is normally the row's last touch)
    features_shipped = rows(con.execute(
        "SELECT feature_id, title, updated_at FROM roadmap "
        "WHERE roadmap_status='shipped'" + window.format(col="updated_at") +
        " ORDER BY updated_at DESC", wparams))
    specs_shipped = rows(con.execute(
        "SELECT d.document_id, d.title, d.frozen_date, r.title AS feature_title "
        "FROM documents d LEFT JOIN roadmap r ON r.feature_id=d.feature_id "
        "WHERE d.kind='spec' AND d.frozen=1" + window.format(col="d.frozen_date") +
        " ORDER BY d.frozen_date DESC", wparams))
    # outstanding is a CURRENT-state number, never window-scoped: a shipped
    # feature with no doc-kind document yet (the docs-pending debt)
    docs_outstanding = rows(con.execute(
        "SELECT r.feature_id, r.title FROM roadmap r "
        "WHERE r.roadmap_status='shipped' AND NOT EXISTS "
        "(SELECT 1 FROM documents d WHERE d.feature_id=r.feature_id AND d.kind='doc') "
        "ORDER BY r.updated_at DESC"))
    # sprint_ref → tracker-doc title, for the session list's sprint clusters
    sprint_titles = {r["sprint_ref"]: r["title"] for r in con.execute(
        "SELECT DISTINCT a.sprint_ref, d.title FROM shell_memory_archives a "
        "LEFT JOIN documents d ON CAST(d.document_id AS TEXT) = a.sprint_ref "
        "WHERE a.sprint_ref IS NOT NULL")}
    return {"favorite_models": sorted(fav.values(), key=lambda f: f["flavor"]),
            "features_shipped": features_shipped, "specs_shipped": specs_shipped,
            "docs_outstanding": docs_outstanding, "sprint_titles": sprint_titles}


def get_analytics_filters(con) -> dict:
    def distinct(col):
        return [r[0] for r in con.execute(
            f"SELECT DISTINCT {col} FROM session_token_usage "
            f"WHERE {col} IS NOT NULL ORDER BY {col}")]
    shells = rows(con.execute(
        "SELECT DISTINCT s.shell_id, s.shortname FROM session_token_usage u "
        "JOIN shells s ON s.shell_id=u.shell_id ORDER BY s.shortname"))
    return {"harnesses": distinct("harness"), "providers": distinct("provider"),
            "models": distinct("model"), "shells": shells}


# ── Mutations ─────────────────────────────────────────────────────────────────

def patch_columns(con, table, pk_col, pk, body, allowed):
    # Column names come exclusively from `allowed` (caller-supplied hardcoded set).
    # Values are kept in a separate list so taint from body never reaches the
    # SQL string — only the parameterised bindings.
    cols = [col for col in sorted(allowed) if col in body]
    if not cols:
        return False, "no editable fields in payload"
    vals = [body[col] for col in cols]
    sets = ", ".join(f"{col}=?" for col in cols)
    cur = con.execute(f"UPDATE {table} SET {sets} WHERE {pk_col}=?",
                      tuple(vals) + (pk,))
    if cur.rowcount == 0:
        return False, "not found"
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


def patch_shell(con, shell_id, body):
    """PATCH /api/shells/{id}. A display_name change (fixing a name that got
    wonked at creation) also re-stamps the system_prompt H1 — but ONLY when the
    H1 still exactly carries the creation-time render (`# <old name> — …`).
    That prefix is shell_factory machinery output, not shell curation; anything
    the shell has since made its own no longer matches and is never touched."""
    if "display_name" in body:
        dn = body["display_name"]
        if not isinstance(dn, str) or not dn.strip():
            return False, "display_name must be a non-empty string"
        body["display_name"] = dn = dn.strip()
        r = con.execute(
            "SELECT display_name, system_prompt FROM shells WHERE shell_id=?",
            (shell_id,)).fetchone()
        if r is None:
            return False, "not found"
        old_h1 = f"# {r['display_name']} — "
        if r["system_prompt"].startswith(old_h1):
            con.execute(
                "UPDATE shells SET system_prompt=? WHERE shell_id=?",
                (f"# {dn} — " + r["system_prompt"][len(old_h1):], shell_id))
    return patch_columns(con, "shells", "shell_id", shell_id, body,
                         SHELL_EDITABLE)


def patch_document(con, doc_id, body):
    r = con.execute("SELECT frozen FROM documents WHERE document_id=?",
                    (doc_id,)).fetchone()
    if r is None:
        return False, "no such document"
    if r["frozen"]:
        return False, "document is frozen — open the next spec, don't edit this one"
    # render_path is editable (#312): a doc authored without one could never
    # be made publishable — `doc edit --render-path` advertised the option
    # and silently dropped it, and `doc add` always INSERTs a new row.
    return patch_columns(con, "documents", "document_id", doc_id, body,
                          {"body", "title", "render_path"})


def _sprint_doc_status(con, doc_id) -> "str | None":
    """The sprint board's `status:` line (the planner is its only writer)."""
    r = con.execute("SELECT body FROM documents WHERE document_id=?",
                    (doc_id,)).fetchone()
    if r is None or r[0] is None:
        return None
    for line in r[0].splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


def _close_sprint_wake(con, doc_id) -> int:
    """Sprint close integration (spec #20 Sprint Scope, sprint 25 seq 10):
    closing (status: CLOSED) or freezing a sprint doc releases its wake
    bindings and cancels their queued wake work in the SAME transaction —
    no orphan armed binding or stranded queued batch survives the close
    (the frozen-CANCEL in the submit gate is the in-flight backstop, not
    the cleanup). Messages stay unread; the Interface chat is untouched.
    Returns the number of bindings released."""
    if con.execute(
            "SELECT 1 FROM sprint_planner_bindings "
            "WHERE sprint_doc_id=? AND released_at IS NULL LIMIT 1",
            (doc_id,)).fetchone() is None:
        return 0
    return len(interface_broker.release_bindings_for_sprint(
        con, doc_id, "sprint closed"))


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


def resolve_project(con, spec):
    """shortname|id → projects row (or None). Shells assign work-streams by name."""
    if str(spec).isdigit():
        return con.execute(
            "SELECT project_id, shortname FROM projects WHERE project_id=? "
            "AND COALESCE(is_deleted,0)=0", (int(spec),)).fetchone()
    return con.execute(
        "SELECT project_id, shortname FROM projects WHERE LOWER(shortname)=LOWER(?) "
        "AND COALESCE(is_deleted,0)=0", (spec,)).fetchone()


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
    "seed_skills": ("Seed skills", "Upsert assets/skills/ into the live DB "
                    "(+ regenerate the seed migration — source repo only). Run "
                    "after authoring or editing a skill body.",
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


# ONE serialization boundary for every path that writes the non-atomic
# snapshot/render outputs (content.sql + the flat-render mirror) or moves the
# main checkout's branches: mem doc writes (serialize_doc_write), the header
# '/api/snapshot' shortcut, and '/api/publish' (git_publish). These were once
# per-path private locks — a doc write could interleave its content.sql/render
# writes with a Publish's branch checkout/staging (SC-012). Held at the caller
# level only: run_snapshot_render() itself never takes it, so nothing re-enters.
#
# SINGLE-WRITER CONSTRAINT: this is an in-process lock only. It is sufficient
# because rendered artifacts are created solely by manual admin-shell or GUI
# actions — no concurrent writers exist in real use. Cross-process locking,
# bounded lock-queueing, and concurrent-Publish races are explicitly out of
# scope for v1 (FnB decision #20; tracked as roadmap #21). Do not add writers
# outside this process without revisiting that decision.
_CONTENT_WRITE_LOCK = threading.Lock()


def serialize_doc_write() -> dict:
    """Re-snapshot + re-render after a mem doc write, so `sc mem doc add/edit/
    freeze` lands on disk headlessly — the git-tracked flat render and
    .sc-state/content.sql move with the write, no GUI Publish (subfloor#434).
    The API is the admin surface (run_script sets SC_ADMIN), and a doc write
    is rare enough that the synchronous pair costs nothing that matters.
    Never raises: the DB write is already committed, so a serialize failure
    comes back as {"ok": False, ...} for the caller to surface instead."""
    with _CONTENT_WRITE_LOCK:
        try:
            return {"ok": True, "output": run_snapshot_render()}
        except RuntimeError as e:
            return {"ok": False, "output": str(e)}


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
# push/PR is skipped, with a clear message. Concurrent publishes — and every
# other content-write path — serialize on _CONTENT_WRITE_LOCK (one git index,
# one set of snapshot outputs), taken by the /api/publish endpoint.
BASE_BRANCH = "main"
PUBLISH_BRANCH = "sc_gui_content"
_STASH_MSG = "sc-publish: stray non-content work"
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
        # Restore any stray non-content work stashed by _prepare_branch — only now,
        # once we're back on base, so it lands where it was taken from. Non-content
        # files are identical across base/publish tips, so pop can't conflict; if it
        # somehow does, keep the stash and tell the operator loudly rather than drop.
        if state.get("stashed"):
            n = state["stashed"]
            pop = _git("stash", "pop")
            if pop.returncode == 0:
                out.append(f"(restored {n} stashed non-content file(s))")
            else:
                out.append(f"⚠ {n} stashed non-content file(s) NOT restored — run "
                           f"'git stash pop' manually:\n{pop.stderr.strip()}")
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
    # Stash real, non-regenerable changes out of the tree for the duration of the
    # publish — that's user work, not publishable content, and the branch moves
    # below would otherwise carry it along (or, if pre-staged, `git commit` would
    # sweep it into the content commit). Stashing isolates it; `git_publish`'s
    # finally pops it back after landing on base. A stray dirty file no longer
    # wedges publish (#283); it's restored untouched once publish is done.
    unexpected = _unexpected_dirty()
    if unexpected:
        st = _git("stash", "push", "-m", _STASH_MSG, "--", *unexpected)
        if st.returncode != 0:
            # Can't isolate the work — fall back to refusing rather than risk it.
            state["ok"] = False
            out.append("✗ working tree has non-content changes and they could not "
                       "be stashed — refusing to publish (commit or stash them "
                       "first):\n"
                       + "\n".join(f"  {p}" for p in unexpected[:20])
                       + ("\n  …" if len(unexpected) > 20 else "")
                       + f"\n  (git stash failed: {st.stderr.strip()})")
            return False
        state["stashed"] = len(unexpected)
        out.append(f"(stashed {len(unexpected)} non-content file(s); "
                   "restored after publish)")

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

    def _send(self, code, payload, ctype="application/json", headers=None):
        body = (json.dumps(payload, default=_json_default)
                if ctype.startswith("application/json")
                else payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
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
        # Write contention on the shared engine DB is not a fault of either
        # side — it's a retryable condition (#331: multi-shell sprint load
        # exhausts busy_timeout and SQLite raises 'database is locked').
        # Nothing was committed (the con rolls back on close), so tell the
        # client to retry instead of leaking the raw sqlite error as a 500.
        if isinstance(exc, db_driver.OperationalError) and (
                "locked" in str(exc) or "busy" in str(exc)):
            log_event("busy", ok=False, path=getattr(self, "path", "?"),
                      detail=[str(exc)])
            return self._send(503, {"error": "engine DB busy — retry",
                                    "retry_after": 2},
                              headers={"Retry-After": "2"})
        traceback.print_exc()
        # Also land it in the rolling log so a failed request is visible after the
        # fact, not only in stderr that may have scrolled away / not been captured.
        log_event("error", ok=False, path=getattr(self, "path", "?"),
                  detail=traceback.format_exc().strip().splitlines()[-15:])
        return self._send(500, {"error": str(exc)})

    # -- Bearer auth helpers --

    def _bearer_token(self) -> str:
        """Extract the raw Bearer token from the Authorization header, or ''."""
        authz = self.headers.get("Authorization", "")
        if authz[:7].lower() == "bearer ":
            return authz[7:].strip()
        return ""

    def _resolve_shell(self) -> tuple:
        """Resolve a Bearer token to a shell_id.

        Returns (shell_id, bad) where:
          bad=False, shell_id=None  — no token presented
          bad=True,  shell_id=None  — token presented but matched no shell → 401
          bad=False, shell_id=int   — valid token, shell resolved
        """
        token = self._bearer_token()
        if not token:
            return None, False
        con = db()
        try:
            row = con.execute(
                "SELECT shell_id FROM shells "
                "WHERE api_key=? AND COALESCE(is_deleted,0)=0",
                (token,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None, True
        return row[0], False

    def _require_shell_auth(self):
        """Enforce Bearer auth — call at the top of any token-scoped route.

        Returns shell_id (int) on success. On failure, sends the 401 response
        and returns None — the caller must return immediately without further
        processing."""
        shell_id, bad = self._resolve_shell()
        if bad:
            self._send(401, {"error": "invalid or unknown token"})
            return None
        if shell_id is None:
            self._send(401, {"error": "Authorization: Bearer <token> required"})
            return None
        return shell_id

    # -- /mem/* token-scoped shell memory endpoints --

    def _mem_get(self, path: str):
        sid = self._require_shell_auth()
        if sid is None:
            return
        parts = path.strip("/").split("/")  # e.g. ["_sc","mem","documents","7"]
        con = db()
        try:
            if path == "/_sc/mem/whoami":
                r = con.execute(
                    "SELECT shell_id, shortname, display_name FROM shells WHERE shell_id=?",
                    (sid,)).fetchone()
                return self._send(200, dict(r) if r else {"shell_id": sid})

            if path == "/_sc/mem/state":
                r = con.execute("SELECT current_state FROM shells WHERE shell_id=?",
                                (sid,)).fetchone()
                return self._send(200, {"current_state": (r[0] if r else None)})

            if path in ("/_sc/mem/seed", "/_sc/mem/lns"):
                kind = "seed" if path.endswith("/seed") else "lns"
                entries = rows(con.execute(
                    "SELECT entry_id, kind, body, entry_date, source_tag "
                    "FROM shell_identity_entries "
                    "WHERE shell_id=? AND kind=? AND COALESCE(is_deleted,0)=0 "
                    "AND retired_at IS NULL ORDER BY entry_date, entry_id",
                    (sid, kind)))
                return self._send(200, {"entries": entries})

            if path == "/_sc/mem/decisions":
                # Index, not library (#274): the log grows unbounded and the
                # planning skills pull it every session. Default = ACTIVE rows
                # only (superseded ones are history, not live constraints), no
                # rationale, newest-first, capped — with counts so the client
                # can say what was hidden. ?all=1 = full log incl. superseded
                # (still no rationale); /decisions/<id> = the full row.
                #
                # Shared on READ (#318/#340, the flags precedent): decisions
                # coordinate the project — a planner's design lock cited in a
                # kickoff message must resolve from every seat, or shells
                # accuse each other of phantom citations. Rows are tagged with
                # the author's shortname; writes stay token-scoped.
                q = parse_qs(urlparse(self.path).query)
                if q.get("all", ["0"])[0] in ("1", "true"):
                    ds = rows(con.execute(
                        "SELECT d.decision_id, d.decision, d.priority, d.decision_date, "
                        "d.parent_decision_id, "
                        "(SELECT s.shortname FROM shells s WHERE s.shell_id=d.shell_id) "
                        " AS shortname, "
                        "(SELECT c.decision_id FROM shell_decisions c "
                        " WHERE c.parent_decision_id=d.decision_id "
                        " AND COALESCE(c.is_deleted,0)=0 "
                        " ORDER BY c.decision_id DESC LIMIT 1) AS superseded_by "
                        "FROM shell_decisions d "
                        "WHERE COALESCE(d.is_deleted,0)=0 "
                        "ORDER BY d.decision_date, d.decision_id"))
                    return self._send(200, {"decisions": ds, "all": True})
                active_sql = (
                    "FROM shell_decisions d "
                    "WHERE COALESCE(d.is_deleted,0)=0 "
                    "AND NOT EXISTS (SELECT 1 FROM shell_decisions c "
                    " WHERE c.parent_decision_id=d.decision_id "
                    " AND COALESCE(c.is_deleted,0)=0)")
                total_active = con.execute(
                    "SELECT COUNT(*) " + active_sql).fetchone()[0]
                superseded = con.execute(
                    "SELECT COUNT(*) FROM shell_decisions d "
                    "WHERE COALESCE(d.is_deleted,0)=0 "
                    "AND EXISTS (SELECT 1 FROM shell_decisions c "
                    " WHERE c.parent_decision_id=d.decision_id "
                    " AND COALESCE(c.is_deleted,0)=0)").fetchone()[0]
                ds = rows(con.execute(
                    "SELECT d.decision_id, d.decision, d.priority, d.decision_date, "
                    "d.parent_decision_id, "
                    "(SELECT s.shortname FROM shells s WHERE s.shell_id=d.shell_id) "
                    " AS shortname " + active_sql +
                    " ORDER BY d.decision_id DESC LIMIT ?",
                    (DECISIONS_INDEX_CAP,)))
                return self._send(200, {"decisions": ds,
                                        "total_active": total_active,
                                        "superseded": superseded})

            if len(parts) == 4 and parts[2] == "decisions":
                # Single decision WITH rationale — the library half of the split.
                # Fleet-wide by id (#318/#340): cross-shell citations resolve.
                did = int(parts[3])
                r = con.execute(
                    "SELECT d.decision_id, d.decision, d.rationale, d.priority, "
                    "d.decision_date, d.parent_decision_id, "
                    "d.feature_id, d.document_id, "
                    "(SELECT s.shortname FROM shells s WHERE s.shell_id=d.shell_id) "
                    " AS shortname, "
                    "(SELECT title FROM roadmap WHERE feature_id=d.feature_id) "
                    " AS feature_title, "
                    "(SELECT title FROM documents WHERE document_id=d.document_id) "
                    " AS document_title, "
                    "(SELECT c.decision_id FROM shell_decisions c "
                    " WHERE c.parent_decision_id=d.decision_id "
                    " AND COALESCE(c.is_deleted,0)=0 "
                    " ORDER BY c.decision_id DESC LIMIT 1) AS superseded_by "
                    "FROM shell_decisions d "
                    "WHERE d.decision_id=? "
                    "AND COALESCE(d.is_deleted,0)=0", (did,)).fetchone()
                if r is None:
                    return self._send(404, {"error": "no such decision"})
                return self._send(200, {"decision": dict(r)})

            if path == "/_sc/mem/flags":
                # Shared: flags coordinate the project, not one shell's memory —
                # return every open flag in the fleet, tagged with its author's
                # shortname so a caller can tell whose blocker it is.
                fs = rows(con.execute(
                    "SELECT f.flag_id, f.display_name, f.priority, f.description, "
                    "f.feature_id, f.created_date, s.shortname AS owner FROM flags f "
                    "LEFT JOIN shells s ON s.shell_id=f.shell_id "
                    "WHERE COALESCE(f.resolved,0)=0 AND COALESCE(f.is_deleted,0)=0 "
                    "ORDER BY f.created_date, f.flag_id"))
                return self._send(200, {"flags": fs})

            if path == "/_sc/mem/roadmap":
                # The board is shared, not per-shell — return all live features.
                rm = rows(con.execute(
                    "SELECT feature_id, title, roadmap_status, summary, project_id, "
                    "sort_order FROM roadmap WHERE roadmap_status != 'retired' "
                    "ORDER BY sort_order, feature_id"))
                return self._send(200, {"roadmap": rm})

            if path == "/_sc/mem/narrative":
                r = con.execute(
                    "SELECT a.full_narrative FROM shells s "
                    "JOIN shell_memory_archives a ON a.archive_id = s.active_archive_id "
                    "WHERE s.shell_id=?", (sid,)).fetchone()
                return self._send(200, {"narrative": (r[0] if r else None)})

            if path == "/_sc/mem/messages":
                # ?direction=sent — the caller's OUTBOUND view (#333): after an
                # ambiguous send timeout, "check-before-resend" needs a way to
                # see whether the write landed. Default stays the inbox.
                q = parse_qs(urlparse(self.path).query)
                if q.get("direction", ["inbox"])[0] == "sent":
                    msgs = rows(con.execute(
                        "SELECT m.message_id, m.to_shell_id, "
                        "s.shortname AS to_shortname, m.kind, m.body, "
                        "m.created_at, m.read_at FROM shell_messages m "
                        "JOIN shells s ON s.shell_id = m.to_shell_id "
                        "WHERE m.from_shell_id=? "
                        "ORDER BY m.created_at DESC LIMIT 50", (sid,)))
                    return self._send(200, {"messages": msgs, "direction": "sent"})
                msgs = rows(con.execute(
                    "SELECT message_id, from_shell_id, kind, body, created_at, read_at "
                    "FROM shell_messages WHERE to_shell_id=? "
                    "ORDER BY read_at IS NOT NULL, created_at DESC LIMIT 50",
                    (sid,)))
                return self._send(200, {"messages": msgs})

            # ── shared planning reads (not per-shell, like /roadmap) ──────────
            # The dev cycle is collaborative: a shell authoring a spec, planning
            # tasks, or handing off a review needs to see the shared work-streams,
            # documents, task plans, and the peer roster — none of which are its
            # own private memory. These mirror the raw SELECTs the docs/spec/
            # review skills used to run against shell_db.db, so no shell needs a
            # direct DB path to do its job.

            if path == "/_sc/mem/projects":
                ps = rows(con.execute(
                    "SELECT project_id, shortname, title, status, standing, purpose "
                    "FROM projects WHERE COALESCE(is_deleted,0)=0 ORDER BY shortname"))
                return self._send(200, {"projects": ps})

            if path == "/_sc/mem/shells":
                # Roster — resolve a peer's shortname (e.g. a commit trailer's
                # display_name → shortname for a review handoff) or its flavor.
                # Not secret: shells already address each other by shortname.
                sh = rows(con.execute(
                    "SELECT shell_id, shortname, display_name, flavor FROM shells "
                    "WHERE COALESCE(is_deleted,0)=0 ORDER BY shell_id"))
                return self._send(200, {"shells": sh})

            if path == "/_sc/mem/documents":
                # List documents (no body), with each doc's task_count so the
                # spec skill can tell active (has tasks) from backlog. Optional
                # ?feature=<id> scopes to one feature.
                q = parse_qs(urlparse(self.path).query)
                feat = q.get("feature", [None])[0]
                sql = ("SELECT d.document_id, d.feature_id, d.kind, d.seq, d.title, "
                       "d.frozen, (SELECT COUNT(*) FROM spec_tasks t "
                       "WHERE t.document_id=d.document_id) AS task_count FROM documents d")
                params: tuple = ()
                if feat is not None:
                    sql += " WHERE d.feature_id=?"
                    params = (int(feat),)
                sql += " ORDER BY d.feature_id, d.kind, d.seq"
                return self._send(200, {"documents": rows(con.execute(sql, params))})

            if len(parts) == 4 and parts[2] == "documents":
                # Single document WITH body — the spec skill loads this to read.
                did = int(parts[3])
                r = con.execute(
                    "SELECT document_id, feature_id, kind, seq, title, body, frozen, "
                    "render_path FROM documents WHERE document_id=?", (did,)).fetchone()
                if r is None:
                    return self._send(404, {"error": "no such document"})
                return self._send(200, {"document": dict(r)})

            if path == "/_sc/mem/tasks":
                # A spec's task plan, by ?doc=<id> (the spec skill) or ?feature=<id>.
                q = parse_qs(urlparse(self.path).query)
                doc = q.get("doc", [None])[0]
                feat = q.get("feature", [None])[0]
                if doc is not None:
                    where, params = "document_id=?", (int(doc),)
                elif feat is not None:
                    where, params = "feature_id=?", (int(feat),)
                else:
                    return self._send(400, {"error": "tasks needs ?doc=<id> or ?feature=<id>"})
                ts = rows(con.execute(
                    "SELECT task_id, feature_id, document_id, seq, title, description, "
                    "status, completed_date, resolution_notes FROM spec_tasks WHERE " + where +
                    " ORDER BY seq", params))
                return self._send(200, {"tasks": ts})

            return self._send(404, {"error": "not found"})
        except ValueError:
            return self._send(400, {"error": "invalid id"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def _mem_post(self, path: str, body: dict):
        sid = self._require_shell_auth()
        if sid is None:
            return
        con = db()
        try:
            if path == "/_sc/mem/state":
                con.execute("UPDATE shells SET current_state=? WHERE shell_id=?",
                            ((body.get("body") or ""), sid))
                con.commit()
                return self._send(200, {"ok": True})

            if path == "/_sc/mem/telemetry":
                # Hook ingest (claude SessionEnd, v1): the harness POSTs its
                # session ref at exit; the server validates it points INTO that
                # harness's own data dir (never an arbitrary path), then runs
                # that parser's incremental sweep inline — the just-ended
                # session is exactly what changed. The boot-time sweep remains
                # the backstop for missed hooks.
                harness = (body.get("harness") or "").strip()
                ref = (body.get("harness_session_ref") or "").strip()
                if harness not in token_parsers.HARNESSES:
                    return self._send(400, {"error": f"unknown harness '{harness}'"})
                if not ref:
                    return self._send(400, {"error": "harness_session_ref required"})
                if ref.startswith("/") or ref.startswith("~"):
                    try:
                        mod = __import__(f"token_parsers.{harness}", fromlist=[harness])
                    except ImportError:
                        return self._send(400, {"error": f"no parser for '{harness}'"})
                    # Sanity gate only — the ref is NEVER opened (the sweep
                    # rescans the harness's own data dir); pure string
                    # normalization keeps the user value out of every
                    # filesystem call (CodeQL py/path-injection).
                    base = getattr(mod, "DATA_DIR", None)
                    rp = os.path.normpath(os.path.expanduser(ref))
                    if base is None or not rp.startswith(str(base) + os.sep):
                        return self._send(400, {"error": "ref outside the harness data dir"})
                return self._send(200, analytics.sweep(only=harness, quiet=True))

            if path in ("/_sc/mem/seed", "/_sc/mem/lns"):
                kind = "seed" if path == "/_sc/mem/seed" else "lns"
                b = (body.get("body") or "").strip()
                if not b:
                    return self._send(400, {"error": "body required"})
                cur = con.execute(
                    "INSERT INTO shell_identity_entries "
                    "(shell_id, kind, body, entry_date, source_tag) VALUES (?, ?, ?, ?, ?)",
                    (sid, kind, b,
                     body.get("entry_date") or None,
                     body.get("source_tag") or None))
                con.commit()
                return self._send(201, {"entry_id": cur.lastrowid})

            if path == "/_sc/mem/decisions":
                d = (body.get("decision") or "").strip()
                if not d:
                    return self._send(400, {"error": "decision required"})
                # Optional why-audit links (#0047). document_id is a refinement of
                # feature_id — a doc rolls up to a feature — so when only the doc
                # is given, derive feature_id from it (the audit-by-feature query
                # then works even for a doc-only link). Both validated: a typo'd
                # id would silently break the audit, so 404 instead of bad data.
                feature_id = body.get("feature_id") or None
                document_id = body.get("document_id") or None
                if document_id is not None:
                    doc = con.execute(
                        "SELECT feature_id FROM documents WHERE document_id=?",
                        (document_id,)).fetchone()
                    if doc is None:
                        return self._send(404, {"error": f"no document {document_id}"})
                    if feature_id is None:
                        feature_id = doc["feature_id"]
                if feature_id is not None and con.execute(
                        "SELECT 1 FROM roadmap WHERE feature_id=?",
                        (feature_id,)).fetchone() is None:
                    return self._send(404, {"error": f"no feature {feature_id}"})
                cur = con.execute(
                    "INSERT INTO shell_decisions "
                    "(shell_id, decision, rationale, priority, decision_date, "
                    " parent_decision_id, feature_id, document_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, d,
                     body.get("rationale") or None,
                     body.get("priority") or "M",
                     body.get("decision_date") or None,
                     body.get("parent_decision_id") or None,
                     feature_id, document_id))
                con.commit()
                return self._send(201, {"decision_id": cur.lastrowid,
                                        "feature_id": feature_id,
                                        "document_id": document_id})

            if path == "/_sc/mem/flags":
                desc = (body.get("description") or "").strip()
                if not desc:
                    return self._send(400, {"error": "description required"})
                cur = con.execute(
                    "INSERT INTO flags (shell_id, display_name, description, priority, feature_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid,
                     body.get("display_name") or None,
                     desc,
                     body.get("priority") or "Medium",
                     body.get("feature_id") or None))
                con.commit()
                return self._send(201, {"flag_id": cur.lastrowid})

            if path == "/_sc/mem/roadmap":
                title = (body.get("title") or "").strip()
                if not title:
                    return self._send(400, {"error": "title required"})
                pid = None
                if body.get("project"):  # optional work-stream by shortname|id
                    pr = resolve_project(con, body["project"])
                    if pr is None:
                        return self._send(404, {"error": f"no project '{body['project']}'"})
                    pid = pr["project_id"]
                cur = con.execute(
                    "INSERT INTO roadmap (title, summary, roadmap_status, sort_order, owning_shell, project_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (title,
                     body.get("summary") or None,
                     body.get("roadmap_status") or "brainstorm",
                     body.get("sort_order") or 0,
                     sid, pid))
                con.commit()
                return self._send(201, {"feature_id": cur.lastrowid})

            if path == "/_sc/mem/tasks":
                title = (body.get("title") or "").strip()
                fid, did, seq = body.get("feature_id"), body.get("document_id"), body.get("seq")
                if not title or fid is None or did is None or seq is None:
                    return self._send(400, {"error": "feature_id, document_id, seq, title required"})
                cur = con.execute(
                    "INSERT INTO spec_tasks (feature_id, document_id, seq, title, description, shell_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (int(fid), int(did), int(seq), title,
                     body.get("description") or None, sid))
                con.commit()
                return self._send(201, {"task_id": cur.lastrowid})

            if path == "/_sc/mem/docs":
                # feature_id is OPTIONAL — standalone (feature-less) docs are part
                # of the contract: the docs/onboard skills and `sc mem doc add`
                # document them, and forks carry them. The seq scope is per
                # (feature, kind), with NULL its own scope (`IS ?` matches NULL).
                fid = body.get("feature_id")
                fid = int(fid) if fid is not None else None
                kind = body.get("kind") or "spec"
                seq = body.get("seq")
                if seq is None:  # next seq for this (feature, kind) — mirrors the old CLI
                    seq = con.execute(
                        "SELECT COALESCE(MAX(seq),0)+1 FROM documents "
                        "WHERE feature_id IS ? AND kind=?", (fid, kind)).fetchone()[0]
                cur = con.execute(
                    "INSERT INTO documents (feature_id, kind, seq, title, body, render_path) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (fid,
                     kind,
                     seq,
                     (body.get("title") or "").strip() or None,
                     body.get("body") or None,
                     body.get("render_path") or None))
                con.commit()
                return self._send(201, {"document_id": cur.lastrowid,
                                        "serialize": serialize_doc_write()})

            if path == "/_sc/mem/narrative":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "text required"})
                r = con.execute("SELECT active_archive_id FROM shells WHERE shell_id=?",
                                (sid,)).fetchone()
                aid = r[0] if r else None
                if not aid:
                    return self._send(409, {"error": "no active session archive"})
                row = con.execute(
                    "SELECT full_narrative FROM shell_memory_archives WHERE archive_id=?",
                    (aid,)).fetchone()
                existing = (row[0] or "") if row else ""
                con.execute(
                    "UPDATE shell_memory_archives SET full_narrative=? WHERE archive_id=?",
                    ((existing + "\n" + text) if existing else text, aid))
                con.commit()
                return self._send(200, {"ok": True})

            if path == "/_sc/mem/messages":
                msg = (body.get("body") or "").strip()
                kind = (body.get("kind") or "shell").strip()
                if kind not in MESSAGE_KINDS:
                    return self._send(400, {"error": f"kind must be one of {', '.join(sorted(MESSAGE_KINDS))}"})
                to_sid = body.get("to_shell_id")
                if to_sid is None and body.get("to"):
                    r = con.execute(
                        "SELECT shell_id FROM shells WHERE LOWER(shortname)=LOWER(?) "
                        "AND COALESCE(is_deleted,0)=0", (body["to"],)).fetchone()
                    if r is None and body["to"].strip().lower() == "cartographer":
                        # Role alias (#369–#372): boot docs and skills address the
                        # map-keeper by role, but forks mint shortnames like CART1
                        # — five seats across two forks followed the docs into a
                        # 404. An exact shortname always wins (checked above); the
                        # flavor's singleton trigger guarantees at most one row.
                        r = con.execute(
                            "SELECT shell_id FROM shells WHERE flavor='cartographer' "
                            "AND COALESCE(is_deleted,0)=0").fetchone()
                        if r is None:
                            return self._send(404, {"error": (
                                "no cartographer shell in this fork — create one "
                                "(flavor 'cartographer'), or address a shortname "
                                "from `sc mem get shells`")})
                    if r is None:
                        return self._send(404, {"error": f"recipient shortname '{body['to']}' unknown"})
                    to_sid = r[0]
                if to_sid is None or not msg:
                    return self._send(400, {"error": "to (shortname) or to_shell_id, and body, required"})
                # Idempotent send (#333): a client timeout after the server-side
                # write left the sender unable to tell delivered from lost, and
                # blind resends duplicated fleet-wide. The client stamps each
                # send invocation with a dedupe_key; a resend of the same key
                # returns the original row instead of inserting a twin. The
                # unique index (from_shell_id, dedupe_key) backstops the
                # check-then-insert race.
                dk = (body.get("dedupe_key") or "").strip() or None
                if dk is not None:
                    r = con.execute(
                        "SELECT message_id FROM shell_messages "
                        "WHERE from_shell_id=? AND dedupe_key=?", (sid, dk)).fetchone()
                    if r is not None:
                        return self._send(200, {"message_id": r[0], "duplicate": True})
                try:
                    cur = con.execute(
                        "INSERT INTO shell_messages (from_shell_id, to_shell_id, body, kind, sprint_doc_id, dedupe_key) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, int(to_sid), msg, kind,
                         body.get("sprint_doc_id"), dk))
                    message_id = cur.lastrowid
                    # Transactional wake ingress (spec #20 Event Ingress, seq
                    # 8): the wake item rides the SAME transaction as the
                    # message — unique (binding_id, message_id) dedupes; a
                    # rollback drops both, so no accepted event is ever lost
                    # or double-woken. Ineligible traffic (shell kind,
                    # unscoped, no ACTIVE binding) creates nothing.
                    interface_wake.maybe_create_wake_item(con, message_id)
                    con.commit()
                except db_driver.IntegrityError:
                    r = con.execute(
                        "SELECT message_id FROM shell_messages "
                        "WHERE from_shell_id=? AND dedupe_key=?", (sid, dk)).fetchone()
                    if r is None:
                        raise
                    return self._send(200, {"message_id": r[0], "duplicate": True})
                # The event is durable — signal the wake coordinator (a no-op
                # when the Interface stack or coordinator is down; the next
                # startup pass drains durable queued work regardless).
                interface_wake.notify_message(message_id)
                return self._send(201, {"message_id": message_id})

            if path == "/_sc/mem/projects":
                shortname = (body.get("shortname") or "").strip()
                title = (body.get("title") or "").strip()
                if not shortname or not title:
                    return self._send(400, {"error": "shortname and title required"})
                try:
                    cur = con.execute(
                        "INSERT INTO projects (shortname, title, purpose, standing, status) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (shortname, title, body.get("purpose"), body.get("standing"),
                         body.get("status") or "active"))
                except db_driver.IntegrityError as e:
                    return self._send(409, {"error": str(e)})
                pid = cur.lastrowid
                con.execute(
                    "INSERT INTO project_shells (project_id, shell_id, role) VALUES (?, ?, ?)",
                    (pid, sid, body.get("role")))
                con.commit()
                return self._send(201, {"project_id": pid, "shortname": shortname})

            if path == "/_sc/mem/oriented":
                con.execute("UPDATE shells SET bootstrapped=1 WHERE shell_id=?", (sid,))
                con.commit()
                return self._send(200, {"ok": True})

            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def _mem_patch(self, path: str):
        sid = self._require_shell_auth()
        if sid is None:
            return
        body = self._body()
        parts = path.strip("/").split("/")  # parts[0]='_sc', parts[1]='mem'
        con = db()
        try:
            # PATCH /_sc/mem/identity-entries/{id}/retire
            if len(parts) == 5 and parts[2] == "identity-entries" and parts[4] == "retire":
                eid = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM shell_identity_entries "
                        "WHERE entry_id=? AND shell_id=? AND is_deleted=0",
                        (eid, sid)).fetchone():
                    return self._send(404, {"error": "no such entry"})
                con.execute(
                    "UPDATE shell_identity_entries SET retired_at=datetime('now') WHERE entry_id=?",
                    (eid,))
                con.commit()
                return self._send(200, {"ok": True})

            # PATCH /_sc/mem/flags/{id}
            # Shared: flags coordinate a project, not one shell's memory — any
            # authenticated shell may resolve/edit any flag. Authorship stays
            # on the row's shell_id (set at open).
            if len(parts) == 4 and parts[2] == "flags":
                fid = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM flags WHERE flag_id=? "
                        "AND COALESCE(is_deleted,0)=0", (fid,)).fetchone():
                    return self._send(404, {"error": "no such flag"})
                if body.get("resolved"):
                    body.setdefault("resolved_date", date.today().isoformat())
                ok, err = patch_columns(con, "flags", "flag_id", fid, body,
                                        FLAG_EDITABLE | {"resolved_date"})
                return self._send(200 if ok else 400, {"ok": ok, "error": err})

            # PATCH /_sc/mem/roadmap/{id}
            # Shared board (matches the fleet-wide GET /roadmap): any shell may
            # advance a feature it did not author — planner→dev handoff needs
            # this. owning_shell records the author and is left untouched.
            if len(parts) == 4 and parts[2] == "roadmap":
                fid = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM roadmap WHERE feature_id=?",
                        (fid,)).fetchone():
                    return self._send(404, {"error": "no such feature"})
                # work-stream assignment: shortname|id|none → project_id
                if "project" in body:
                    spec = body.pop("project")
                    if str(spec).lower() in ("none", "-", ""):
                        body["project_id"] = None
                    else:
                        pr = resolve_project(con, spec)
                        if pr is None:
                            return self._send(404, {"error": f"no project '{spec}'"})
                        body["project_id"] = pr["project_id"]
                # dependency set: replace via the cycle-checked helper
                if "blocked_by" in body:
                    ok, err = set_blockers(con, fid, body.pop("blocked_by"))
                    if not ok:
                        return self._send(400, {"ok": False, "error": err})
                    if not body:
                        return self._send(200, {"ok": True})
                ok, err = patch_columns(con, "roadmap", "feature_id", fid,
                                        body, ROADMAP_EDITABLE | {"project_id"})
                return self._send(200 if ok else 400, {"ok": ok, "error": err})

            # PATCH /_sc/mem/projects/{id|shortname}
            if len(parts) == 4 and parts[2] == "projects":
                pr = resolve_project(con, parts[3])
                if pr is None:
                    return self._send(404, {"error": f"no project '{parts[3]}'"})
                ok, err = patch_columns(con, "projects", "project_id", pr["project_id"],
                                        body, {"standing", "status"})
                return self._send(200 if ok else 400, {"ok": ok, "error": err})

            # PATCH /_sc/mem/tasks/{id}
            # Shared: a spec's task plan is collaborative (the builder starts/
            # completes tasks the planner laid in). shell_id records who added
            # the task and is left untouched. 'cancelled' (#342) is the honest
            # terminal state for a task overtaken by a feature split/re-scope;
            # resolution_notes says why (mirrors flag close --notes).
            if len(parts) == 4 and parts[2] == "tasks":
                tid = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM spec_tasks WHERE task_id=?",
                        (tid,)).fetchone():
                    return self._send(404, {"error": "no such task"})
                if "status" in body and body["status"] not in TASK_STATUSES:
                    return self._send(400, {"error": f"status must be one of "
                                            f"{', '.join(sorted(TASK_STATUSES))}"})
                if "title" in body:
                    # same invariant as task add — an edit may not blank the title
                    body["title"] = (body.get("title") or "").strip()
                    if not body["title"]:
                        return self._send(400, {"error": "title must be non-empty"})
                if body.get("status") == "done":
                    body.setdefault("completed_date", date.today().isoformat())
                ok, err = patch_columns(con, "spec_tasks", "task_id", tid,
                                        body, {"status", "title", "description",
                                               "completed_date", "resolution_notes"})
                return self._send(200 if ok else 400, {"ok": ok, "error": err})

            # PATCH /_sc/mem/docs/{id}/freeze — must precede the bare /docs/{id} check
            # Shared: specs/docs are collaborative (matches the fleet-wide GET
            # /documents); any shell may freeze/edit regardless of the feature's
            # authoring shell.
            if len(parts) == 5 and parts[2] == "docs" and parts[4] == "freeze":
                did = int(parts[3])
                r = con.execute(
                    "SELECT frozen FROM documents WHERE document_id=?",
                    (did,)).fetchone()
                if r is None:
                    return self._send(404, {"error": "no such document"})
                if r[0]:
                    # Idempotent (SC-013): a retry after an ambiguous timeout —
                    # the freeze committed but the response was lost — must read
                    # as the success it was, not a 409. The re-serialize also
                    # heals any drift a lost post-freeze serialize left behind.
                    return self._send(200, {"ok": True, "already_frozen": True,
                                            "serialize": serialize_doc_write()})
                con.execute(
                    "UPDATE documents SET frozen=1, frozen_date=date('now') WHERE document_id=?",
                    (did,))
                # Sprint close integration (seq 10): freezing the board
                # releases its wake bindings + cancels queued wake work.
                released = _close_sprint_wake(con, did)
                con.commit()
                return self._send(200, {"ok": True,
                                        "released_bindings": released,
                                        "serialize": serialize_doc_write()})

            # PATCH /_sc/mem/docs/{id}
            if len(parts) == 4 and parts[2] == "docs":
                did = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM documents WHERE document_id=?",
                        (did,)).fetchone():
                    return self._send(404, {"error": "no such document"})
                ok, err = patch_document(con, did, body)
                if not ok:
                    return self._send(400, {"ok": ok, "error": err})
                # Sprint close integration (seq 10): the planner closes the
                # board by setting status: CLOSED — release its wake
                # bindings + cancel queued wake work in the same breath.
                released = 0
                if _sprint_doc_status(con, did) == "CLOSED":
                    released = _close_sprint_wake(con, did)
                    con.commit()
                return self._send(200, {"ok": ok,
                                        "released_bindings": released,
                                        "serialize": serialize_doc_write()})

            # PATCH /_sc/mem/messages/{id}/read
            if len(parts) == 5 and parts[2] == "messages" and parts[4] == "read":
                mid = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM shell_messages WHERE message_id=? AND to_shell_id=?",
                        (mid, sid)).fetchone():
                    return self._send(404, {"error": "no such message"})
                con.execute(
                    "UPDATE shell_messages SET read_at=datetime('now') WHERE message_id=?",
                    (mid,))
                con.commit()
                return self._send(200, {"ok": True})

            return self._send(404, {"error": "not found"})
        except ValueError:
            return self._send(400, {"error": "invalid id"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    # -- static + GET --
    # -- /_sc/watches — the PR watch registry (sprint eventing) --
    # Token-scoped like /_sc/mem/*: registration defaults to the calling shell;
    # `shell` names another subscriber (the sprint skill registers the planner
    # at PR open — the recipient-naming precedent of `message send`). The list
    # is a shared read like /roadmap: single-operator fork, the planner needs
    # the whole board. Since the polling cutover (spec #20 task #85, decision
    # #19) the service's own scheduler is the sole poller and DB writer:
    # registration takes an immediate GitHub baseline (no baseline, no armed
    # watch), `--sprint` scopes a watch to an ACTIVE sprint document, and
    # /_sc/watches/reconcile is the operator's explicit one-shot poll.

    def _daemon_state(self, con) -> "dict | None":
        """Poller liveness (#359): the heartbeat row → age + verdict.
        Stale = beat older than 3× the poller's own interval (one slow gh
        call + the sleep fit comfortably inside). None = never run (or a
        pre-0068 DB with no heartbeat table yet) — the client renders both
        None and stale as "watches are NOT being polled"."""
        try:
            r = con.execute(
                "SELECT beat_at, interval_s, CAST((julianday('now') - "
                "julianday(beat_at)) * 86400 AS INTEGER) AS age_s "
                "FROM daemon_heartbeats WHERE name='watch'").fetchone()
        except Exception:
            return None
        if r is None:
            return None
        return {"beat_at": r["beat_at"], "interval_s": r["interval_s"],
                "age_s": r["age_s"], "stale": r["age_s"] > 3 * r["interval_s"]}

    def _watches_get(self):
        sid = self._require_shell_auth()
        if sid is None:
            return
        con = db()
        try:
            q = parse_qs(urlparse(self.path).query)
            include_closed = q.get("all", ["0"])[0] in ("1", "true", "yes")
            active = pr_poller.active_sprint_doc_ids(con)
            sql = ("SELECT w.watch_id, w.repo, w.pr_number, w.shell_id, "
                   "s.shortname, w.created_at, w.closed_at, w.sprint_doc_id "
                   "FROM watched_prs w "
                   "JOIN shells s ON s.shell_id = w.shell_id")
            if not include_closed:
                sql += " WHERE w.closed_at IS NULL"
            sql += " ORDER BY w.repo, w.pr_number, w.watch_id"
            ws = rows(con.execute(sql))
            for w in ws:
                w["armed"] = (w.get("closed_at") is None
                              and w.get("sprint_doc_id") in active)
            return self._send(200, {"watches": ws,
                                    "daemon": self._daemon_state(con)})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def _watches_post(self, body: dict):
        sid = self._require_shell_auth()
        if sid is None:
            return
        con = db()
        try:
            repo = (body.get("repo") or "").strip().strip("/")
            try:
                pr = int(body.get("pr_number"))
            except (TypeError, ValueError):
                pr = None
            if not repo or repo.count("/") != 1 or pr is None:
                return self._send(400, {"error": "repo (owner/name) and pr_number (int) required"})
            sprint_doc_id = body.get("sprint_doc_id")
            if sprint_doc_id is not None:
                try:
                    sprint_doc_id = int(sprint_doc_id)
                except (TypeError, ValueError):
                    return self._send(400, {"error": "sprint_doc_id must be an int"})
                if not pr_poller.is_active_sprint(con, sprint_doc_id):
                    return self._send(409, {"error":
                        f"document {sprint_doc_id} is not an ACTIVE, unfrozen "
                        "SPRINT doc — watches arm only to active sprints"})
            target = sid
            if body.get("shell"):
                r = con.execute(
                    "SELECT shell_id FROM shells WHERE LOWER(shortname)=LOWER(?) "
                    "AND COALESCE(is_deleted,0)=0", (body["shell"],)).fetchone()
                if r is None:
                    return self._send(404, {"error": f"shell '{body['shell']}' unknown"})
                target = r[0]
            # Idempotent by (repo, pr, shell, scope): a live duplicate in the
            # SAME scope returns the existing watch. A live UNSCOPED watch
            # registered with --sprint is rebound (explicit re-arm — legacy
            # dormant watches become polled only this way). A retired watch is
            # NOT reopened: registration inserts a new row so closed history
            # is retained (0080 cutover).
            existing = con.execute(
                "SELECT watch_id, sprint_doc_id FROM watched_prs "
                "WHERE repo=? AND pr_number=? AND shell_id=? AND closed_at IS NULL "
                "AND COALESCE(sprint_doc_id, 0) = COALESCE(?, 0)",
                (repo, pr, target, sprint_doc_id)).fetchone()
            daemon = self._daemon_state(con)
            if existing is not None:
                return self._send(200, {"watch_id": existing["watch_id"],
                                        "existing": True, "daemon": daemon})
            unscoped = None
            if sprint_doc_id is not None:
                unscoped = con.execute(
                    "SELECT watch_id FROM watched_prs "
                    "WHERE repo=? AND pr_number=? AND shell_id=? "
                    "AND closed_at IS NULL AND sprint_doc_id IS NULL",
                    (repo, pr, target)).fetchone()
            # Registration baseline (spec: an immediate GitHub read, normalized
            # and stored BEFORE arming; a failed baseline creates no armed
            # watch and returns a retryable sanitized error). The gh call
            # happens before the INSERT so a failure leaves no row at all.
            fp, err = pr_poller.baseline_read(repo, pr)
            if fp is None:
                return self._send(502, {"error":
                    f"baseline read failed (retryable): {err}",
                    "retryable": True, "daemon": daemon})
            if unscoped is not None:
                con.execute(
                    "UPDATE watched_prs SET sprint_doc_id=?, last_seen=? "
                    "WHERE watch_id=?",
                    (sprint_doc_id, json.dumps(fp), unscoped["watch_id"]))
                con.commit()
                return self._send(200, {"watch_id": unscoped["watch_id"],
                                        "rebound": True, "daemon": daemon})
            cur = con.execute(
                "INSERT INTO watched_prs (repo, pr_number, shell_id, last_seen, "
                "sprint_doc_id) VALUES (?, ?, ?, ?, ?)",
                (repo, pr, target, json.dumps(fp), sprint_doc_id))
            con.commit()
            return self._send(201, {"watch_id": cur.lastrowid, "daemon": daemon})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def _watches_reconcile(self):
        """Operator's explicit one-shot poll (spec: startup + explicit
        reconciliation are the two non-interval triggers). Synchronous —
        the response IS the cycle's summary."""
        sid = self._require_shell_auth()
        if sid is None:
            return
        con = db()
        try:
            summary = pr_poller.poll_cycle(con, source="reconcile")
            return self._send(200, summary)
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in _STATIC:
            fname, ctype = _STATIC[path]
            f = UI_DIR / fname
            if not f.exists():
                return self._send(404, "not built", "text/plain")
            # Restrictive CSP on the app shell (spec #20 Security): vendored
            # scripts/styles + same-origin connections only, no inline script.
            headers = {"Content-Security-Policy": _CSP} if fname == "index.html" else None
            return self._send(200, f.read_text(), ctype, headers=headers)
        if path.startswith("/_sc/mem/"):
            return self._mem_get(path)
        if path == "/_sc/watches":
            return self._watches_get()
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        # git-hygiene is a live filesystem/git read — no DB, computed on demand
        # (the UI refresh button is the only trigger). `?fetch=1` does the network
        # fetch for accurate behind-counts + fresh PR state; the default skips it
        # so the initial tab load is snappy.
        if urlparse(self.path).path == "/api/git-state":
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
            if path == "/api/flavor-defaults":
                return self._send(200, get_flavor_defaults(con))
            if path == "/api/models":
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, model_catalog.catalog(
                    refresh=q.get("refresh", ["0"])[0] in ("1", "true"),
                    con=con))
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
            if path == "/api/analytics/sessions":
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, get_analytics_sessions(con, q))
            if path == "/api/analytics/tokens":
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, get_analytics_tokens(con, q))
            if path == "/api/analytics/usage":
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, get_analytics_usage(con, q))
            if path == "/api/analytics/filters":
                return self._send(200, get_analytics_filters(con))
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
            if path == "/api/pm2":
                return self._send(200, {"pm2": pm2_mod.read()})
            if path == "/api/pm2/status":
                # Live process view. Needs the host's pm2, so proxy to the
                # pm2-broker in the sandbox; call directly on the no-docker host.
                if os.environ.get("SC_SANDBOX"):
                    try:
                        return self._send(200, pm2_mod.broker_call("GET", "/status"))
                    except ConnectionError:
                        return self._send(503, {
                            "ok": False,
                            "output": "pm2 status needs the host pm2-broker — "
                                      "start it with `./sc pm2-broker-up` on the host."})
                return self._send(200, pm2_mod.do_status())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/_sc/mem/"):
            return self._mem_post(path, self._body())
        if path == "/_sc/watches":
            return self._watches_post(self._body())
        if path == "/_sc/watches/reconcile":
            return self._watches_reconcile()
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
            if path == "/api/flavor-defaults":
                ok, err = set_flavor_default(con, self._body())
                return self._send(200 if ok else 400,
                                  {"ok": ok} if ok else {"error": err})
            if path == "/api/analytics/sweep":
                # GUI Analytics tab load — incremental, so steady-state is
                # cheap; sweep opens its own connection.
                return self._send(200, analytics.sweep(quiet=True))
            if path == "/api/snapshot":
                try:
                    with _CONTENT_WRITE_LOCK:
                        out = run_snapshot_render()
                except Exception as e:
                    # run_snapshot_render raises on a failed serialize/render; log
                    # the failure before re-raising so it's in the rolling log too.
                    log_event("snapshot", ok=False, detail=str(e))
                    raise
                log_event("snapshot", ok=True, detail=out)
                return self._send(200, {"output": out})
            if path == "/api/publish":
                with _CONTENT_WRITE_LOCK:
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
            if path.startswith("/api/pm2/validate/"):
                # One live check against the candidate `pm2` block. The checks
                # run pm2 + curl the app's local port, which only work on the
                # HOST; in the sandbox, proxy to the pm2-broker. Mirror of ts.
                check = path.rsplit("/", 1)[1]
                cfg = self._body().get("pm2") or {}
                if os.environ.get("SC_SANDBOX"):
                    try:
                        r = pm2_mod.broker_call("POST", f"/validate/{check}", {"pm2": cfg})
                    except ConnectionError:
                        return self._send(503, {
                            "ok": False, "check": check,
                            "output": "live checks need the host pm2-broker — start it "
                                      "with `./sc pm2-broker-up` on the host, then retry."})
                    if r.get("error") == "no such check":
                        return self._send(404, {"error": "no such check"})
                    return self._send(200, r)
                r = pm2_mod.validate(check, cfg)
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
        if path.startswith("/_sc/mem/"):
            return self._mem_patch(path)
        body = self._body()
        con = db()
        try:
            if path.startswith("/api/shells/") and path.count("/") == 3:
                sid = int(path.rsplit("/", 1)[1])
                ok, err = patch_shell(con, sid, body)
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
        # pm2 block:    PUT /api/pm2  {pm2: {...}}  (persists to instance.json)
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
            if path == "/api/pm2":
                pb = self._body().get("pm2")
                if pb is not None and not isinstance(pb, dict):
                    return self._send(400, {"error": "pm2 must be an object"})
                return self._send(200, {"ok": True, "pm2": pm2_mod.write(pb)})
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

    def do_DELETE(self):
        # DELETE /api/shells/{id} — soft-delete a shell (flip is_deleted=1).
        # Matches the house pattern (skill.py): every read path filters on
        # COALESCE(is_deleted,0)=0, so this hides the shell everywhere without
        # touching its child rows, and frees the cartographer singleton slot.
        path = urlparse(self.path).path
        con = db()
        try:
            if path.startswith("/api/shells/") and path.count("/") == 3:
                sid = int(path.rsplit("/", 1)[1])
                cur = con.execute(
                    "UPDATE shells SET is_deleted=1 "
                    "WHERE shell_id=? AND COALESCE(is_deleted,0)=0", (sid,))
                con.commit()
                if cur.rowcount == 0:
                    return self._send(404, {"error": "no such shell"})
                return self._send(200, {"ok": True, "shell_id": sid})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._fail(e)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# asyncio transport integration (sprint 25 seq 5, spec #20)
#
# The stdlib ThreadingHTTPServer loop is replaced by api/transport.py's
# one-port HTTP+WS multiplex. Every existing route below is UNTOUCHED: the
# shim re-hydrates a Handler instance from a parsed request and captures its
# response, so the route logic keeps running exactly as written (now on the
# transport's executor threads instead of ThreadingHTTPServer's threads).

class _ShimHandler(Handler):
    """A Handler driven without a socket: the transport feeds the parsed
    request in, response bytes/status are captured instead of written."""

    def __init__(self, method: str, path: str, headers_raw: str, body: bytes):
        # Deliberately NOT super().__init__ (that would run socket handling).
        self.command = method
        self.path = path
        self.requestline = f"{method} {path} HTTP/1.1"
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = http.client.parse_headers(io.BytesIO(headers_raw.encode("latin-1")))
        self._shim_status = 200
        self._shim_headers: list = []

    # -- BaseHTTPRequestHandler response plumbing, captured --------------------
    def log_request(self, code="-", size="-"):  # noqa: D102
        pass

    def send_response_only(self, code, message=None):  # noqa: D102
        self._shim_status = code

    def send_header(self, keyword, value):  # noqa: D102
        self._shim_headers.append((keyword, value))

    def end_headers(self):  # noqa: D102
        pass

    def send_error(self, code, message=None, explain=None):
        self._shim_status = code
        self._shim_headers = [("Content-Type", "application/json")]
        self.wfile = io.BytesIO()
        self.wfile.write(json.dumps({"error": message or code}).encode())

    # log_request/log_message already quiet via Handler.log_message.


def dispatch_http(method: str, path: str, headers_raw: str,
                  body: bytes) -> tuple:
    """The transport's HTTP entry: route one request, return
    (status, [(header, value)], body bytes). Interface API paths go to the
    interface module; everything else runs through the shimmed Handler."""
    parsed = urlparse(path)
    if parsed.path.startswith("/api/interface/") or \
            parsed.path.startswith("/api/planner-action-receipts"):
        if interface_routes is None:
            return (503, [("Content-Type", "application/json")],
                    json.dumps({"error": {
                        "code": "interface_unavailable",
                        "message": "Interface stack not importable on this "
                                   f"server ({_INTERFACE_IMPORT_ERROR})",
                        "details": {}}}).encode())
        return interface_routes.handle(method, path, headers_raw, body)
    handler = _ShimHandler(method, path, headers_raw, body)
    try:
        route = getattr(handler, f"do_{method}", None)
        if route is None:
            handler.send_error(405, "method not allowed")
        else:
            route()
    except Exception as exc:  # noqa: BLE001 — mirrors the old server's per-request isolation
        traceback.print_exc()
        handler._shim_status = 500
        handler._shim_headers = [("Content-Type", "application/json")]
        handler.wfile = io.BytesIO(str(exc).encode())
    return (handler._shim_status, handler._shim_headers,
            handler.wfile.getvalue())


async def _ws_unavailable(reader, writer, head_raw: bytes) -> None:
    writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                 b"Connection: close\r\n\r\n")
    try:
        await writer.drain()
    finally:
        writer.close()


def main(argv):
    port = None
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if port is None:
        port = ports_mod.resolve().get("port", 8800)
    if not DB_PATH.exists():
        sys.exit(f"server: no DB at {DB_PATH} — run `./sc rebuild` first.")
    # Provision API keys at startup: every shell needs an api_key to reach this
    # server's token-scoped routes, but shells created before migration 0027 (or
    # on a fork that never ran the one-off backfill) come up NULL-keyed and would
    # silently fall back to direct-DB. The running API owns key provisioning — so
    # ensure it here, idempotently, on every boot. Reuses the same minting the
    # auth path resolves against; a `make launch/restart` thus self-heals keys
    # (no separate `./sc update` step). New shells are still keyed at creation.
    backfill_shell_api_keys.backfill(str(DB_PATH))
    # Interface startup reconciliation (spec #20): idempotent, once per boot —
    # parks any crash-window pending input as delivery_unknown (never
    # replays), recovers wake batches from durable hook-sequence evidence,
    # repairs expired reservations, revokes stale writer leases. No-ops on a
    # pre-0078 DB.
    con = db_driver.connect(DB_PATH)
    try:
        recon = interface_reconcile.startup_reconcile(con)
    finally:
        con.close()
    if recon.get("parks") or recon.get("batches_delivery_unknown"):
        print(f"server: interface reconcile {recon}")
    # Watched-PR poller (spec #20 task #85, decision #19): this service is the
    # fork's SOLE GitHub poller — the legacy host `sc watch daemon` (direct-DB
    # writer) is retired by the same commit that enables this scheduler, which
    # is the cutover gate: no second writer can be started by any supervised
    # path. Bounded interval only while ACTIVE sprint watches exist, plus the
    # startup pass inside the thread; self-disables when gh is absent.
    pr_poller.Poller(DB_PATH).start()
    # Bind 127.0.0.1 by default (the host stance: localhost-only, operator owns
    # network controls). In the container set SC_BIND=0.0.0.0 so docker can
    # publish the port — the jail is the `-p 127.0.0.1:PORT:PORT` mapping, which
    # keeps it loopback-only on the host regardless of the in-container bind.
    bind = os.environ.get("SC_BIND", "127.0.0.1")
    import transport  # noqa: E402  (api/ — asyncio one-port multiplex)

    async def _serve():
        ws_handler = _ws_unavailable
        runtime = None
        if interface_ws is not None:
            runtime = interface_ws.build_runtime(db_path=str(DB_PATH))
            # Bind BEFORE start(): start() reattaches survivors and walks lost
            # sessions through the routes callback, so it must be set first.
            interface_routes.bind_runtime(runtime)
            interface_routes.ensure_operator_capability()
            await runtime.start()
            ws_handler = runtime.handle_ws
        else:
            print(f"server: Interface unavailable ({_INTERFACE_IMPORT_ERROR}) "
                  "— review UI only", file=sys.stderr)
        try:
            await transport.serve(bind, port, dispatch_http, ws_handler)
        finally:
            if runtime is not None:
                await runtime.stop()

    print(f"super-coder review layer → http://127.0.0.1:{port}  (bind {bind}, DB: {DB_PATH.name})")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
