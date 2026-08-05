#!/usr/bin/env python3
"""super-coder review layer — a localhost server.

One process serves the JSON API and the static review UI on a single per-fork
port (see scripts/ports.py + api/transport.py). The review layer stays
zero-dependency stdlib; the transport pins `websockets` for its one-port
multiplex (WS upgrades answer unavailable). Single-user, localhost — network
controls are the operator's, exactly like superCC's API surface.

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
import hashlib
import http.client
import io
import ipaddress
import json
import os
import sqlite3
import subprocess
import sys
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

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
import artifact_policy  # noqa: E402
import backfill_shell_api_keys  # noqa: E402  (startup key provisioning)
import conversation_broker  # noqa: E402  (Feature #24 durable turn service)
import conversation_launch  # noqa: E402  (canonical shell launch preparation)
import conversation_reaper  # noqa: E402  (Feature #31 orphan process ladder)
import db_driver  # noqa: E402
import git_hygiene  # noqa: E402  (live repo dirty/stale/clean snapshot)
import mem_credentials  # noqa: E402  (runtime Admin credential provisioning, spec #30 req 11)
import sprint_close  # noqa: E402  (Sprints v2 conformance + report evidence)
import sprint_domain  # noqa: E402  (Sprints v2 work dispatch authority)
import sprint_liveness  # noqa: E402  (Sprints v2 one-shot monitor surface)
import sprint_message_delivery  # noqa: E402  (Sprints v2 inbox acceptance)
import sprint_recovery  # noqa: E402  (Sprints v2 pause/resume reconciliation)
import sprint_review_loop  # noqa: E402  (Sprints v2 Dev/Review command surface)
import sprint_runtime  # noqa: E402  (Sprint dispatch + engine wake delivery)
import sprint_board  # noqa: E402  (read-only Sprints v2 FnB board projections)
import skill_projection  # noqa: E402  (exact bounded grant mirrors)
sys.path.insert(0, str(ENGINE / "api"))
import conversation_routes  # noqa: E402  (Feature #24 browser conversations)
import review_routes  # noqa: E402  (Feature #26 browser Diff review)
import map_db  # noqa: E402  (read-only handle to the dr_* catalogue in map.db)
import ports as ports_mod  # noqa: E402
import shell_factory  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402  (engine_skill_names — origin rule)
import sprint_participant_chats  # noqa: E402  (registry-backed Sprint wake chats)
import sprint_pr_watcher  # noqa: E402  (engine-wide PR subscription observation)
import model_catalog  # noqa: E402  (live model-id suggestions, sibling module)
import analytics  # noqa: E402  (token & session analytics sweep — doc #11)
import token_parsers  # noqa: E402  (harness roster + per-parser data dirs)
from quota_probes import dispatch as quota_dispatch  # noqa: E402  (account quota probes — doc #49)
import vm as vm_mod  # noqa: E402  (Windows Test VM — config + live checks)
import ts as ts_mod  # noqa: E402  (tailnet — config + live checks)
import pm2 as pm2_mod  # noqa: E402  (host pm2 stack — config + live checks)

# The app SHELL stays a frozen route table (spec #48): four files, a closed set
# that has not changed in the life of the project, and index.html is where the
# CSP header hangs. Adding one is a route change its author is already looking
# at. Vendored assets are the opposite population — see _VENDOR_TYPES below.
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Vendored assets resolve PER REQUEST against `ui/vendor/` (spec #48). The
# frozen table above ages at a different rate than the file bodies it points
# at: bodies are read from disk every request, so pulling the repo under a
# running server updated every registered asset instantly while a NEWLY
# vendored one stayed unreachable until a restart. That shipped a current
# app.js to a browser against a server that would not serve its dependencies —
# a state neither version was tested in (the 2026-07-25 GUI outage: xterm's
# addon-fit.js on disk, 404 from the route table).
#
# This mapping is the ALLOWLIST the frozen table used to be — the second job it
# was quietly doing, and the one any replacement has to keep. Suffix decides the
# content type; nothing is ever sniffed from bytes. `.map` is deliberately
# absent (ruled this round): a source map is a debugging affordance this
# loopback GUI does not need to serve, and widening this dict is a filesystem
# tenancy decision, not a convenience one.
_VENDOR_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def _resolve_vendor(rel: str) -> tuple:
    """Resolve one `/vendor/` request path to a servable file.

    `rel` is the ALREADY-DECODED remainder of the URL path. Returns
    `(Path, content-type)` on a hit and `(None, <the gate that said no>)` on a
    miss — the miss reason is served to the operator, because the SHAPE of a
    404 was the entire diagnosis in the incident this route comes from.

    Gate order is the security contract, not a style choice: decoding precedes
    containment (checking first and decoding after is the classic hole), and
    resolution precedes the bound check (which is what makes a symlink out of
    the tree fail the same way `..` does).
    """
    root = (UI_DIR / "vendor").resolve()
    try:
        ctype = _VENDOR_TYPES.get(Path(rel).suffix.lower())
        if ctype is None:
            return None, "suffix not allowlisted"
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root):
            return None, "outside the vendor root"
        # Regular files only: no directory listings, no devices, and no bare
        # `/vendor/` prefix.
        if not candidate.is_file():
            return None, "no such file"
    except (OSError, ValueError):
        # A path the filesystem itself refuses to interpret — NUL bytes, a
        # name too long, a symlink loop — is a miss, not a server fault. The
        # read below stays OUTSIDE this guard on purpose: a file that passed
        # every gate and still cannot be read is a real fault and must be
        # loud, not a 404 claiming it was never there.
        return None, "unresolvable path"
    return candidate, ctype

# The localhost authorities for the socket sources in the CSP below.
_CSP_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _csp(port: int) -> str:
    """Content-Security-Policy for the app shell (spec #20 Security And
    Privacy; spec #26 Trust Boundary, where same-origin script execution is
    operator-equivalent and this header is therefore release-critical):
    vendored scripts and same-origin connections only.

    `connect-src` names the socket origins EXPLICITLY. It used to read
    `'self' ws: wss:`, and conformance finding SC-152 was right that this
    contradicted the "same-origin only" claim standing next to it: `ws:` and
    `wss:` are CSP *scheme-sources*, and a scheme-source matches any host
    using that scheme (https://www.w3.org/TR/CSP3/#match-url-to-source-expression).
    That left injected same-origin script an outbound WebSocket channel to
    anywhere — the exact containment the policy exists to provide. Naming
    `scheme://host:port` closes it to this server's own origins.

    The socket origins are listed rather than left to `'self'` alone because
    `'self'` matching ws/wss arrived late and unevenly across browsers, and
    the terminal stream is not a feature to lose to a browser-version
    difference. Styles keep 'unsafe-inline' — the no-build UI sets style
    attributes via DOM and the doc renderer emits inline styling; scripts
    stay strict.
    """
    sockets = " ".join(f"{scheme}://{host}:{port}"
                       for host in _CSP_HOSTS for scheme in ("ws", "wss"))
    return ("default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {sockets}; img-src 'self' data:; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'")


# Rebound in main() once the real port resolves — the socket sources are
# port-exact, so a fork on a non-default port would otherwise refuse its own
# terminal stream. This default only covers a shell rendered without main().
_CSP = _csp(8800)

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
# display_name is editable (#288): it was settable exactly once, at open, so a
# flag opened without a name could never be given one — and an unnamed flag is
# the one referred to by bare integer, which is the precondition for #149's
# id/name collision. Same reasoning as SHELL_EDITABLE above.
FLAG_EDITABLE = {"resolved", "resolution_notes", "description", "display_name",
                 "feature_id", "priority"}
ROADMAP_EDITABLE = {"title", "roadmap_status", "summary", "sort_order", "project_id"}

# Typed traffic on the generic shell_messages bus.
MESSAGE_KINDS = {"shell", "task", "result"}
# spec_tasks lifecycle — 'cancelled' (#342) closes a task whose work moved in
# a feature split/re-scope without lying that it was built. Validated here so
# a typo'd status is a 400, not a raw CHECK-constraint 500.
TASK_STATUSES = {"pending", "in_progress", "done", "cancelled"}


def db():
    return db_driver.connect(DB_PATH)


def require_current_schema(db_path=DB_PATH,
                           migrations_dir=ENGINE / "migrations") -> None:
    """Fail loudly before new engine code touches an older DB schema.

    A pre-fix updater can materialize the target engine and pin before its
    live-state refusal fires.  The newly installed server must therefore
    recognize that half-applied floor from the migration ledger before key
    provisioning or request handling reaches a column the old DB does not have.
    """
    expected = {path.name for path in migrations_dir.glob("*.sql")}
    con = db_driver.connect(db_path)
    try:
        table = con.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = set() if table is None else {
            row[0] for row in con.execute(
                "SELECT filename FROM schema_migrations")
        }
    except db_driver.OperationalError:
        applied = set()
    finally:
        con.close()

    missing = sorted(expected - applied)
    if not missing:
        return
    names = ", ".join(missing)
    ref_path = REPO_ROOT / ".sc-state" / "engine.ref"
    ref = ref_path.read_text().strip()[:12] if ref_path.exists() else "un-pinned"
    sys.exit(
        "server: installed engine/DB schema mismatch — "
        f"installed engine {ref} requires migrations that "
        f"{Path(db_path).name} has not applied; its ledger is missing "
        f"{len(missing)} required "
        f"migration(s): {names}\n"
        "Refusing startup before first DB use; continuing would run new code "
        "against an old schema.\n"
        "Recovery: run `./sc rollback --engine-only` to restore the previous "
        "engine/pin while preserving this unchanged DB, then retry "
        "`./sc update`.")


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
    shells = rows(con.execute(
        "SELECT s.shell_id, s.display_name, s.shortname, s.role, s.flavor, "
        "s.mandate, s.is_shared, "
        "(SELECT COUNT(*) FROM shell_messages m "
        " WHERE m.to_shell_id=s.shell_id AND m.read_at IS NULL "
        " AND m.kind IN ('shell','task','result')) AS unread_message_count, "
        "(SELECT w.available_at FROM sprint_wake_outbox w "
        " WHERE w.receiver_shell_id=s.shell_id AND w.state='pending' "
        " AND w.available_at>datetime('now')) AS pending_wake_available_at "
        "FROM shells s WHERE COALESCE(s.is_deleted,0)=0 ORDER BY s.shell_id"))
    return sprint_participant_chats.attach_live_participations(con, shells)


def get_shell(con, sid: int) -> dict | None:
    r = con.execute(
        "SELECT shell_id, display_name, shortname, partner, role, mandate, "
        "system_prompt, current_state, lineage_seed, "
        "has_identity, active_archive_id, flavor FROM shells "
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
        "(SELECT 1 FROM resolved_shell_skills ss "
        " WHERE ss.shell_id=? AND ss.skill_id=s.skill_id) "
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
    """The catalogue + flavor/Bespoke grants for Shells → Skill Assignments.
    Grouping into sections (repo / category) happens client-side, like
    flags/docs."""
    skills = rows(con.execute(
        "SELECT skill_id, name, description, category, command, common "
        "FROM skills WHERE is_deleted=0 ORDER BY name"))
    tag_origin(skills)
    shell_grants: dict[int, list] = {}
    for g in rows(con.execute(
            "SELECT ss.skill_id, ss.shell_id FROM shell_skills ss "
            "JOIN shells sh ON sh.shell_id=ss.shell_id "
            "WHERE sh.flavor IS NULL AND COALESCE(sh.is_deleted,0)=0 "
            "ORDER BY ss.shell_id")):
        shell_grants.setdefault(g["skill_id"], []).append(g["shell_id"])
    flavor_grants: dict[int, list] = {}
    for g in rows(con.execute(
            "SELECT skill_id, flavor FROM flavor_skills "
            "ORDER BY flavor, skill_id")):
        flavor_grants.setdefault(g["skill_id"], []).append(g["flavor"])
    for s in skills:
        s["granted_shells"] = shell_grants.get(s["skill_id"], [])
        s["granted_flavors"] = flavor_grants.get(s["skill_id"], [])
    flavors = [
        {"flavor": t["flavor"], "role": t.get("role")}
        for t in shell_factory.flavors()
    ]
    return {"skills": skills, "shells": get_shells(con), "flavors": flavors}


def get_cli_skills(con) -> dict:
    """Exact read projection used by authenticated ``sc skill list`` calls."""
    skills = rows(con.execute(
        "SELECT skill_id, name, common, is_deleted FROM skills "
        "ORDER BY is_deleted, name"
    ))
    scopes: dict[int, list[str]] = {}
    for row in rows(con.execute(
        "SELECT skill_id, 'flavor:' || flavor AS scope FROM flavor_skills "
        "UNION ALL "
        "SELECT ss.skill_id, 'shell:' || "
        "COALESCE(sh.shortname, sh.display_name, sh.shell_id) AS scope "
        "FROM shell_skills ss JOIN shells sh ON sh.shell_id=ss.shell_id "
        "WHERE sh.flavor IS NULL AND COALESCE(sh.is_deleted,0)=0 "
        "ORDER BY scope"
    )):
        scopes.setdefault(row["skill_id"], []).append(row["scope"])
    for skill in skills:
        skill["grant_scopes"] = scopes.get(skill["skill_id"], [])
    return {"skills": skills}


def get_model_routes(con, *, harness: str | None = None,
                     selector: str | None = None) -> dict:
    """Small exact-route projection for authenticated shell CLI reads."""
    sql = (
        "SELECT harness, selector, source, availability, stale, "
        "headless_supported, high_effort_supported, cli_version, "
        "supported_efforts FROM model_routes"
    )
    clauses = []
    params = []
    if harness is not None:
        clauses.append("harness=?")
        params.append(harness)
    if selector is not None:
        clauses.append("selector=?")
        params.append(selector)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY harness, availability='available' DESC, selector"
    return {"routes": rows(con.execute(sql, tuple(params)))}


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


def model_route_available(con, harness: str, selector: str) -> bool:
    """True only for an exact route proved available by the local catalogue."""
    row = con.execute(
        "SELECT 1 FROM model_routes WHERE harness=? AND selector=? "
        "AND availability='available' AND stale=0",
        (harness, selector)).fetchone()
    return row is not None


def set_flavor_default(con, body) -> tuple[bool, str | None]:
    """One write to the launch-defaults matrix: set a (flavor, harness) cell's
    model, and/or star the harness as the flavor's default. Starring is
    transactional across the flavor's rows — exactly one is_default=1 after.
    Upserts the row so template flavors / harnesses without a seeded row are
    settable; a null model clears the cell back to Harness default."""
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
    model = None
    if "model" in body:
        raw_model = body.get("model")
        if raw_model is not None and (
                not isinstance(raw_model, str) or not raw_model.strip()):
            return False, (
                "invalid_model_route: model must be null for Harness default "
                "or an exact non-empty available route")
        model = raw_model.strip() if isinstance(raw_model, str) else None
        if model is not None and not model_route_available(
                con, harness, model):
            return False, (
                f"invalid_model_route: {model!r} is not an exact currently "
                f"available route for {harness}; choose an available "
                "model or Harness default")
    con.execute(
        "INSERT INTO flavor_defaults (flavor, harness, model, is_default) "
        "VALUES (?, ?, NULL, 0) ON CONFLICT(flavor, harness) DO NOTHING",
        (flavor, harness))
    if "model" in body:
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
# models — enriched with shell identity via the attributed archive.

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
            f"SELECT a.archive_id, a.session_id, s.shortname, "
            f"s.display_name, s.flavor FROM shell_memory_archives a "
            f"JOIN shells s ON s.shell_id=a.shell_id WHERE a.archive_id IN ({marks})",
            aids)}
    for c in cards:
        a = arch.get(c["archive_id"]) or {}
        c["shell"] = a.get("shortname")
        c["shell_session"] = a.get("session_id")
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
    return {"favorite_models": sorted(fav.values(), key=lambda f: f["flavor"]),
            "features_shipped": features_shipped, "specs_shipped": specs_shipped,
            "docs_outstanding": docs_outstanding}


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


# ── Provider quota (spec doc #57, superseding #49's account panel) ───────────
# Token analytics answers *what did we spend*; this answers *how much is left*.
# It calls quota_probes.dispatch.probe_all ONCE and owns nothing that call does:
# concurrency, the 5s per-probe timeout and one-provider-failure containment all
# live in the dispatcher, because containment is only checkable from the probe
# package. A second timeout layer here would be a bug, not defence in depth.
#
# The route is `quota`, never `usage` and no longer `accounts`. /analytics/usage
# already means token spend on this tab, and a second meaning on that word is a
# defect waiting to happen; `accounts` named the thing this response stopped
# carrying when decision #75 dropped account identity, and a path that describes
# a response's old shape misleads exactly the reader who trusts it.

QUOTA_TTL_SECONDS = 60      # toggling the two sections must not hammer three
                            # third-party endpoints; the refresh button bypasses it

# The TTL's clock: the last probe ATTEMPT, in this process — never the newest
# captured_at. A probe that identifies nobody (no credential file → `na`, or a
# provider erroring before it can name an account) writes NO rows, so a DB clock
# never advances and every arrival would re-probe, breaking the spec's own
# "toggling sections twice inside a minute performs one probe" in exactly the
# degraded case. The attempt is recorded whether or not it identifies anybody.
#
# Living in process, it RESETS ON A SERVER RESTART — the spec declares that
# ("a restart storm re-probes") and it is load-bearing, not merely tolerated:
# `providers` below dies with the same process, so the first arrival after a
# restart MUST probe or the response would carry an EMPTY per-provider status
# list, and the panel could not tell "nothing configured" from "every probe
# failed". Mixing a DB clock back in would reopen exactly that window. The claim
# is taken under the lock and before the probe runs, so two simultaneous
# arrivals collapse to one probe rather than racing to fire two.
_QUOTA_LOCK = threading.Lock()
_QUOTA_PROBE: dict = {"at": None, "providers": []}

# The conflict target is the EXPRESSION, character for character as migration
# 0096 declares the index: SQLite matches an upsert target against the index
# expression, not against a column list. Naming plain `scope` here raises "ON
# CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint" — so a
# mismatch fails loud instead of silently duplicating the panel's account-wide
# rows (session, weekly and five_hour are ALL scope-NULL) on every single probe.
_QUOTA_WINDOW_UPSERT = """
INSERT INTO harness_quota_window
    (account_pk, window_kind, scope, used_percent, used, limit_value,
     resets_at, captured_at, status, probe_version)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(account_pk, window_kind, COALESCE(scope, '')) DO UPDATE SET
    used_percent=excluded.used_percent, used=excluded.used,
    limit_value=excluded.limit_value, resets_at=excluded.resets_at,
    captured_at=excluded.captured_at, status=excluded.status,
    probe_version=excluded.probe_version
"""


def _quota_claim(force: bool) -> bool:
    """True when THIS caller should probe. `force` is the refresh button."""
    now = datetime.now(timezone.utc).timestamp()
    with _QUOTA_LOCK:
        last = _QUOTA_PROBE["at"]
        if not force and last and now - last < QUOTA_TTL_SECONDS:
            return False
        _QUOTA_PROBE["at"] = now
        return True


def _quota_upsert(con, accounts: list[dict]) -> int:
    """Land one probe_all result. Returns the window rows written.

    An account carrying no account_ref writes NO registry row: that is a
    provider with no credential file (`na` — the absence of a limit is not a
    limit of zero) or one that failed before it could name anyone. Such a row
    could never be matched again, and it holds no reading to show.

    THE REGISTRY ROW IS NOW NOTHING BUT A KEY — (account_pk, provider,
    account_ref) — so the upsert has nothing to update and DOES NOTHING on
    conflict. Everything it used to carry described WHICH ACCOUNT, which is the
    question a provider-level panel stops asking: account_label and is_current
    went with the cards that read them, plan is displayed nowhere and returned
    nowhere, and first_seen/last_seen lost their last reader when the newest
    reading came to be selected by the WINDOW's captured_at (see
    `_latest_readings`) rather than by the row's last_seen.

    Keeping them as cheap provenance would have been the wrong instinct:
    THESE TABLES ARE PROBE-REBUILDABLE CACHES, and provenance a single probe
    regenerates is not provenance (decision #75, migration 0097)."""
    named = [a for a in accounts if a.get("account_ref")]
    written = 0
    for acct in named:
        seen = acct["captured_at"]
        con.execute(
            "INSERT INTO harness_quota_account (provider, account_ref) "
            "VALUES (?, ?) ON CONFLICT(provider, account_ref) DO NOTHING",
            (acct["provider"], acct["account_ref"]))
        pk = con.execute(
            "SELECT account_pk FROM harness_quota_account WHERE provider=? AND account_ref=?",
            (acct["provider"], acct["account_ref"])).fetchone()[0]
        # An account with no windows (`unauth`, or an error after identification)
        # writes none, so the last known values stand with their own age — the
        # card reports staleness rather than a measured zero.
        for w in acct.get("windows") or []:
            con.execute(_QUOTA_WINDOW_UPSERT, (
                pk, w["window_kind"], w.get("scope"), w.get("used_percent"),
                w.get("used"), w.get("limit_value"), w.get("resets_at"),
                w.get("captured_at") or seen, w.get("status") or "ok",
                w.get("probe_version")))
            written += 1
    return written


def probe_quota_accounts(con) -> dict:
    """Probe every provider and land the result. One call to probe_all; the
    dispatcher never raises, so a dead provider costs its own card and nothing
    else."""
    notes: list[str] = []
    accounts = quota_dispatch.probe_all(notes.append)
    written = _quota_upsert(con, accounts)
    con.commit()
    # Per-provider status, including the providers that produced no registry row
    # at all: without it "no accounts" is indistinguishable from "every probe
    # failed", and the panel cannot tell the operator which one it is.
    providers = [{"provider": a["provider"], "status": a.get("status"),
                  "detail": a.get("detail")} for a in accounts]
    with _QUOTA_LOCK:
        _QUOTA_PROBE["providers"] = providers
    return {"providers": providers, "windows_written": written, "notes": notes}


def _latest_readings(con) -> dict:
    """provider -> {captured_at, windows[]}: the most recent reading each
    provider has produced, whichever account produced it.

    KEYED ON THE WINDOW'S captured_at, NOT the registry row's last_seen, and
    that is load-bearing rather than a detail. A row with no windows has no
    reading, so it cannot outrank one that has numbers — which is what makes
    the stale registry rows flag #196 minted from a guessed account_ref both
    unable to win and impossible to see, without a data migration to hunt them
    down. It is also literally what spec #57 asks for: the most recent reading
    by captured_at, regardless of which account produced it.

    Multiple accounts for one provider are not disambiguated. Under a
    provider-level panel "which account am I looking at" is not a question the
    surface answers, and decision #68's multi-account problem dissolves with
    it.

    A READING IS ONE CAPTURE, NOT EVERY WINDOW EVER SEEN. The window upsert
    updates and never deletes, so a kind the provider stops reporting keeps its
    row for good — and returning the accumulated set would file that row under
    the newest capture's age. That is spec #57's second empty-state wall
    crossed exactly: an hour-old figure presented as fresh under an "as of 1m
    ago" stamp. Every window of one probe run carries that run's captured_at
    (the probes stamp it once), so the newest capture's rows are precisely the
    rows whose captured_at equals the newest. A probe that failed writes no
    rows at all, which is why the degraded card still shows its whole last
    known reading — with that reading's own age."""
    groups: dict = {}
    for w in rows(con.execute(
            "SELECT a.provider AS provider, w.* FROM harness_quota_window w "
            "JOIN harness_quota_account a ON a.account_pk = w.account_pk "
            "ORDER BY w.window_kind, w.scope")):
        groups.setdefault((w.pop("provider"), w.pop("account_pk")), []).append(w)
    latest: dict = {}
    for (provider, _pk), windows in groups.items():
        captured_at = max(w["captured_at"] for w in windows)
        reading = [w for w in windows if w["captured_at"] == captured_at]
        if provider not in latest or captured_at > latest[provider]["captured_at"]:
            latest[provider] = {"captured_at": captured_at, "windows": reading}
    return latest


def get_analytics_quota(con, force: bool = False) -> dict:
    """One entry per provider — its windows, the age of the reading, and its
    status — probing first when the last probe ATTEMPT has aged past the TTL.
    `force` is POST /probe, the refresh button.

    NOTHING IN THIS RESPONSE IDENTIFIES THE OPERATOR: no label, no email, no
    account ref, no plan, no sign-in state (decision #75). account_ref stays in
    the DB as the upsert key and stops there.

    The entry list is built from the probe package's PROVIDERS, not from
    whatever the status list or the registry happens to hold. A provider that
    has never been probed must still render a card reading "no reading yet" —
    the operator has to be able to tell "not configured" from "not readable",
    and a list built from observed rows cannot express the first."""
    probe = probe_quota_accounts(con) if _quota_claim(force) else None
    latest = _latest_readings(con)
    # The per-provider status list KEEPS ITS MEANING and its source: it is what
    # distinguishes "nothing configured" from "the probe failed", and dropping
    # identity does not merge those two.
    status = {p["provider"]: p for p in
              ((probe or {}).get("providers") or list(_QUOTA_PROBE["providers"]))}
    providers = []
    for name in quota_dispatch.PROVIDERS:
        reading = latest.get(name) or {}
        providers.append({
            "provider": name,
            "status": status.get(name, {}).get("status"),
            "detail": status.get(name, {}).get("detail"),
            # The age of the numbers on the card, and it is never omitted: it
            # is the only thing that tells a stale reading from a fresh one.
            "captured_at": reading.get("captured_at"),
            "windows": reading.get("windows") or [],
        })
    return {"providers": providers,
            "ttl_seconds": QUOTA_TTL_SECONDS,
            "probed": probe is not None,
            "notes": (probe or {}).get("notes") or []}


# ── Mutations ─────────────────────────────────────────────────────────────────

def patch_columns(con, table, pk_col, pk, body, allowed, commit=True):
    # Column names come exclusively from `allowed` (caller-supplied hardcoded set).
    # Values are kept in a separate list so taint from body never reaches the
    # SQL string — only the parameterised bindings.
    cols = [col for col in sorted(allowed) if col in body]
    if not cols:
        return False, "no editable fields in payload"
    vals = [body[col] for col in cols]
    sets = ", ".join(f"{col}=?" for col in cols)
    try:
        cur = con.execute(f"UPDATE {table} SET {sets} WHERE {pk_col}=?",
                          tuple(vals) + (pk,))
    except db_driver.IntegrityError as e:
        # A trigger refused the write (e.g. the current_state length cap). Its
        # message routes the fix, so hand it back as a client error instead of
        # letting it surface as an unhandled 500 with the remedy buried.
        con.rollback()
        return False, str(e)
    if cur.rowcount == 0:
        return False, "not found"
    if commit:
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


def patch_document(con, doc_id, body, commit=True):
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
                         {"body", "title", "render_path"}, commit=commit)


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


def set_grant(con, sid, skill_id, granted) -> tuple[bool, str | None]:
    shell = con.execute(
        "SELECT flavor FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
        (sid,)).fetchone()
    if shell is None:
        return False, "no such shell"
    if shell["flavor"] is not None:
        return False, (
            f"shell belongs to flavor '{shell['flavor']}' — edit the flavor pack")
    if con.execute(
            "SELECT 1 FROM skills WHERE skill_id=? AND is_deleted=0",
            (skill_id,)).fetchone() is None:
        return False, "no such skill"
    if granted:
        con.execute("INSERT OR IGNORE INTO shell_skills (shell_id, skill_id) "
                    "VALUES (?, ?)", (sid, skill_id))
    else:
        con.execute("DELETE FROM shell_skills WHERE shell_id=? AND skill_id=?",
                    (sid, skill_id))
    con.commit()
    return True, None


def _known_flavors(con) -> set[str]:
    return {t["flavor"] for t in shell_factory.flavors()} | {
        r[0] for r in con.execute(
            "SELECT DISTINCT flavor FROM shells WHERE flavor IS NOT NULL")
    } | {
        r[0] for r in con.execute("SELECT DISTINCT flavor FROM flavor_skills")
    }


def set_flavor_grant(con, flavor, skill_id, granted) -> tuple[bool, str | None]:
    if flavor not in _known_flavors(con):
        return False, f"unknown flavor '{flavor}'"
    if con.execute(
            "SELECT 1 FROM skills WHERE skill_id=? AND is_deleted=0",
            (skill_id,)).fetchone() is None:
        return False, "no such skill"
    if granted:
        con.execute(
            "INSERT OR IGNORE INTO flavor_skills (flavor, skill_id) VALUES (?, ?)",
            (flavor, skill_id))
    else:
        con.execute(
            "DELETE FROM flavor_skills WHERE flavor=? AND skill_id=?",
            (flavor, skill_id))
    con.commit()
    return True, None


# Whitelisted maintenance scripts runnable from the GUI. Each is a fixed argv —
# the GUI passes only a registry KEY, never a command, so nothing arbitrary runs.
# Order = display order; `danger` ones prompt for confirmation in the UI.
_PY = sys.executable
_ARTIFACT_DEST = artifact_policy.content_path().relative_to(REPO_ROOT)
_SCRIPTS = {
    "snapshot": ("Snapshot", f"Serialize the per-instance tables → {_ARTIFACT_DEST} "
                 "(deterministic, idempotent). Run after editing identity, roadmap, "
                 "docs, or flags so the change survives a rebuild.",
                 [_PY, str(ENGINE / "scripts/snapshot.py")], False),
    "render": ("Render flat", "Regenerate the flat _sc files under the active artifact root "
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


def health_payload() -> dict:
    """Runtime identity plus the local artifact actions the UI may offer."""
    cfg = ports_mod.resolve()
    return {
        "ok": True,
        "repo": cfg.get("repo"),
        "port": cfg.get("port"),
        "artifact_mode": artifact_policy.mode(),
        "git_publication": False,
    }


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
    """The header 'save locally ⤓' shortcut — serialize then render."""
    snap = run_script("snapshot")
    if not snap["ok"]:
        raise RuntimeError("snapshot failed:\n" + snap["output"])
    rend = run_script("render")
    if not rend["ok"]:
        raise RuntimeError("render failed:\n" + rend["output"])
    return (snap["output"] + "\n" + rend["output"]).strip()


# ONE serialization boundary for every path that writes the non-atomic
# snapshot/render outputs (content.sql + the flat-render mirror): mem doc writes
# (serialize_doc_write) and the header '/api/snapshot' shortcut. Held at the
# caller level only: run_snapshot_render() itself never takes it.
#
# SINGLE-WRITER CONSTRAINT: this is an in-process lock only. It is sufficient
# because rendered artifacts are created solely by manual admin-shell or GUI
# actions — no concurrent writers exist in real use. Do not add writers outside
# this process without first adding an appropriate cross-process lock.
_CONTENT_WRITE_LOCK = threading.Lock()


def serialize_doc_write() -> dict:
    """Re-snapshot + re-render after a mem doc write, so `sc mem doc add/edit/
    freeze` lands in the gitignored local artifact cache headlessly.
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


def _git(*args, env=None):
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, env=env)


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
    """Compatibility tombstone: generated-artifact Git publication is retired."""
    raise RuntimeError(
        "Git publication of generated artifacts is retired; save locally instead"
    )
    # The legacy implementation remains below for one upgrade window so old
    # installations can be diagnosed, but is deliberately unreachable.
    if not artifact_policy.tracks_local_artifacts():
        output = run_snapshot_render()
        message = (
            f"{output}\n✓ artifact_mode=local — state is durable under "
            ".sc-state/local/; no Git branch, commit, or PR was created"
        )
        log_event("publish", ok=True, pushed=False, pr_url=None, detail=message)
        return {"ok": True, "output": message, "pr_url": None, "local": True}
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
    # Engine-initiated commit = deliberate home maintenance by definition; the
    # pre-commit home-repo guard (work_repo installs) must not block publish.
    c = _git("commit", "-m", msg,
             env={**os.environ, "SC_HOME_MAINTENANCE": "1"})
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
        # A local-only home substrate (remotes removed on purpose) publishes to
        # disk + local git only — that is success, not an error.
        out.append("✓ committed locally; no 'origin' remote — push/PR skipped "
                   "(local-only repo)")
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
        # Bytes pass through untouched. Vendored assets are not all text — a
        # str-only sender would force the allowlist to exclude fonts and
        # images, which is a trap for the next person to vendor one.
        if isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = (json.dumps(payload, default=_json_default)
                    if ctype.startswith("application/json")
                    else payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        # HEAD is the identical response minus the body, Content-Length
        # included (RFC 9110 9.3.2) — so the answer to "is this served?" is
        # generated from the same code that serves it and cannot drift.
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _not_modified(self, headers: dict):
        """304: the validators and cache directives, no body (RFC 9110 15.4.5)."""
        self.send_response(304)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def _if_none_match(self, etag: str) -> bool:
        """Does the client already hold this exact representation?

        Weak comparison, as RFC 9110 13.1.2 requires for If-None-Match: the
        `W/` prefix never affects the match, and the header carries a LIST —
        matching only a whole-header equality would 200 every browser that
        sends more than one tag.
        """
        raw = self.headers.get("If-None-Match", "")
        if not raw:
            return False
        candidates = [c.strip() for c in raw.split(",")]
        if "*" in candidates:
            return True
        return any(c.removeprefix("W/") == etag for c in candidates)

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
        # side — it's a retryable condition (#331: concurrent shell load
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

    def _require_browser_operator(self, con):
        """Accept the loopback browser operator and reject shell credentials."""
        token = self._bearer_token()
        if token:
            shell = con.execute(
                "SELECT 1 FROM shells WHERE api_key=? AND COALESCE(is_deleted,0)=0",
                (token,),
            ).fetchone()
            if shell is None:
                self._send(401, {"error": {
                    "code": "unauthorized",
                    "message": "invalid browser authorization",
                    "details": {},
                }})
            else:
                self._send(403, {"error": {
                    "code": "fnb_operator_required",
                    "message": "the Sprint board is owned by the browser FnB operator",
                    "details": {},
                }})
            return False
        operator = con.execute(
            "SELECT 1 FROM users WHERE is_active=1 ORDER BY user_id LIMIT 1"
        ).fetchone()
        if operator is None:
            self._send(401, {"error": {
                "code": "unauthorized",
                "message": "no active browser operator exists",
                "details": {},
            }})
            return False
        return True

    def _sprint_board_error(self, exc: Exception):
        if isinstance(exc, sprint_board.ProjectionError):
            return self._send(exc.status, {"error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }})
        return self._fail(exc)

    def _require_browser_mutation_origin(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host") or ""
        if origin:
            parsed = urlparse(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.netloc != host
                or parsed.path
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                self._send(403, {"error": {
                    "code": "same_origin_required",
                    "message": "Sprint lifecycle actions require the browser origin",
                    "details": {},
                }})
                return False
        if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
            self._send(403, {"error": {
                "code": "same_origin_required",
                "message": "Sprint lifecycle actions require the browser origin",
                "details": {},
            }})
            return False
        return True

    def _sprint_board_mutation_error(self, exc: Exception):
        if isinstance(exc, sprint_domain.SprintAuthorityError):
            status, code = 403, "forbidden"
        elif isinstance(
            exc, (sprint_domain.SprintStateError, sprint_domain.SprintInvariantError)
        ):
            status, code = 409, "lifecycle_conflict"
        elif isinstance(exc, KeyError):
            status, code = 404, "sprint_not_found"
        else:
            return self._fail(exc)
        return self._send(status, {"error": {
            "code": code,
            "message": str(exc).strip("'"),
            "details": {},
        }})

    # -- /sprint/* token-scoped collaboration endpoints --

    @staticmethod
    def _sprint_integer(body: dict, name: str) -> int:
        value = body.get(name)
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a positive integer")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @classmethod
    def _sprint_integer_list(cls, body: dict, name: str) -> list[int]:
        values = body.get(name)
        if not isinstance(values, list) or not values:
            raise ValueError(f"{name} must be a non-empty array")
        return list(
            dict.fromkeys(
                cls._sprint_integer({name: item}, name) for item in values
            )
        )

    def _sprint_error(self, exc: Exception):
        if isinstance(exc, sprint_domain.SprintAuthorityError):
            return self._send(403, {"error": str(exc)})
        if isinstance(exc, sprint_domain.SprintPreflightError):
            return self._send(422, {"error": str(exc)})
        if isinstance(exc, sprint_domain.SprintInvariantError):
            return self._send(409, {"error": str(exc)})
        if isinstance(exc, KeyError):
            return self._send(404, {"error": str(exc).strip("'")})
        if isinstance(exc, (ValueError, TypeError)):
            return self._send(400, {"error": str(exc)})
        return self._fail(exc)

    @staticmethod
    def _require_sprint_planner(con, sprint_id: int, shell_id: int) -> None:
        sprint = con.execute(
            "SELECT originating_planner_shell_id FROM sprints WHERE sprint_id=?",
            (sprint_id,),
        ).fetchone()
        if sprint is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if int(sprint["originating_planner_shell_id"]) != shell_id:
            raise sprint_domain.SprintAuthorityError(
                "only the owning Planner may trigger Sprint dispatch or monitoring"
            )

    @staticmethod
    def _sprint_planner_proxy(con, sprint_id: int, shell_id: int) -> int:
        row = con.execute(
            "SELECT sp.originating_planner_shell_id,caller.flavor "
            "FROM sprints sp JOIN shells caller ON caller.shell_id=? "
            "WHERE sp.sprint_id=?",
            (shell_id, sprint_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        planner_shell_id = int(row["originating_planner_shell_id"])
        if shell_id != planner_shell_id and row["flavor"] != "admin":
            raise sprint_domain.SprintAuthorityError(
                "only the owning Planner or FnB may change the Sprint plan"
            )
        return planner_shell_id

    @staticmethod
    def _sprint_actor(
        con, sprint_id: int, shell_id: int
    ) -> sprint_domain.LifecycleActor:
        row = con.execute(
            "SELECT caller.flavor,sp.originating_planner_shell_id,"
            "EXISTS(SELECT 1 FROM sprint_participants p "
            "WHERE p.sprint_id=sp.sprint_id "
            "AND p.shell_id=caller.shell_id) participates "
            "FROM sprints sp JOIN shells caller ON caller.shell_id=? "
            "WHERE sp.sprint_id=?",
            (shell_id, sprint_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Sprint: {sprint_id}")
        if row["flavor"] == "admin":
            return sprint_domain.LifecycleActor("fnb", shell_id)
        if int(row["originating_planner_shell_id"]) == shell_id:
            return sprint_domain.LifecycleActor("planner", shell_id)
        if row["participates"]:
            return sprint_domain.LifecycleActor("participant", shell_id)
        raise sprint_domain.SprintAuthorityError(
            "only a Sprint participant or FnB may change lifecycle"
        )

    def _declare_sprint(self, con, shell_id: int, body: dict) -> int:
        feature_id = self._sprint_integer(body, "feature_id")
        approval_ids = self._sprint_integer_list(body, "spec_approval_ids")
        participants = body.get("participants")
        if not isinstance(participants, list) or not participants:
            raise ValueError("participants must be a non-empty array")
        if body.get("merge_grant_enabled") is not True:
            raise sprint_domain.SprintInvariantError(
                "declaration requires an explicit Sprint merge grant"
            )

        caller = con.execute(
            "SELECT flavor FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
            (shell_id,),
        ).fetchone()
        requested_planner = body.get("planner_shell_id")
        planner_shell_id = (
            shell_id
            if requested_planner is None
            else self._sprint_integer(body, "planner_shell_id")
        )
        if caller is None or (
            caller["flavor"] != "admin"
            and (caller["flavor"] != "planner" or planner_shell_id != shell_id)
        ):
            raise sprint_domain.SprintAuthorityError(
                "only the originating Planner or FnB may declare a Sprint"
            )
        planner = con.execute(
            "SELECT 1 FROM shells WHERE shell_id=? AND flavor='planner' "
            "AND COALESCE(is_deleted,0)=0",
            (planner_shell_id,),
        ).fetchone()
        if planner is None:
            raise sprint_domain.SprintInvariantError(
                "originating Planner must be an active Planner shell"
            )

        normalized: list[dict] = []
        seen_shells: set[int] = set()
        for participant in participants:
            if not isinstance(participant, dict):
                raise ValueError("each participant must be an object")
            participant_shell_id = self._sprint_integer(
                participant, "shell_id"
            )
            role = participant.get("role")
            harness = participant.get("harness")
            if role not in {"planner", "developer", "reviewer"}:
                raise ValueError(
                    "participant role must be planner, developer, or reviewer"
                )
            if not isinstance(harness, str) or not harness.strip():
                raise ValueError("participant harness is required")
            if participant_shell_id in seen_shells:
                raise ValueError("participant shells must be unique")
            seen_shells.add(participant_shell_id)
            normalized.append(
                {
                    "shell_id": participant_shell_id,
                    "role": role,
                    "harness": harness.strip(),
                    "model": participant.get("model"),
                    "effort": participant.get("effort"),
                    "route": participant.get("route"),
                }
            )
        if not any(
            item["shell_id"] == planner_shell_id and item["role"] == "planner"
            for item in normalized
        ):
            raise sprint_domain.SprintInvariantError(
                "originating Planner must be a Planner participant"
            )

        with db_driver.write_transaction(con, "sprint.declare"):
            if con.execute(
                "SELECT 1 FROM roadmap WHERE feature_id=?", (feature_id,)
            ).fetchone() is None:
                raise KeyError(f"unknown feature: {feature_id}")
            approval_rows = []
            for approval_id in approval_ids:
                approval = con.execute(
                    "SELECT a.document_id,a.revision_sha256,a.verdict,d.feature_id,"
                    "d.kind,d.body,reviewer.flavor AS reviewer_flavor,"
                    "COALESCE(reviewer.is_deleted,0) AS reviewer_deleted "
                    "FROM sprint_spec_approvals a "
                    "JOIN documents d ON d.document_id=a.document_id "
                    "JOIN shells reviewer ON reviewer.shell_id=a.reviewer_shell_id "
                    "WHERE a.approval_id=?",
                    (approval_id,),
                ).fetchone()
                if approval is None:
                    raise KeyError(f"unknown Sprint spec approval: {approval_id}")
                current_revision = hashlib.sha256(
                    approval["body"].encode()
                ).hexdigest()
                if (
                    approval["verdict"] != "pass"
                    or approval["kind"] != "spec"
                    or int(approval["feature_id"]) != feature_id
                    or approval["revision_sha256"] != current_revision
                    or approval["reviewer_flavor"] != "reviewer"
                    or approval["reviewer_deleted"]
                ):
                    raise sprint_domain.SprintInvariantError(
                        "declaration requires current passing approvals for its feature"
                    )
                approval_rows.append((approval_id, approval))
            active_shells = {
                int(row["shell_id"]): str(row["flavor"])
                for row in con.execute(
                    "SELECT shell_id,flavor FROM shells "
                    "WHERE COALESCE(is_deleted,0)=0"
                )
            }
            if not seen_shells.issubset(active_shells.keys()):
                raise sprint_domain.SprintInvariantError(
                    "Sprint participants must be active shells"
                )
            expected_flavors = {
                "planner": "planner",
                "developer": "dev",
                "reviewer": "reviewer",
            }
            if any(
                active_shells[item["shell_id"]] != expected_flavors[item["role"]]
                for item in normalized
            ):
                raise sprint_domain.SprintInvariantError(
                    "Sprint participant roles must match their shell flavors"
                )
            sprint_id = int(
                con.execute(
                    "INSERT INTO sprints "
                    "(feature_id,originating_planner_shell_id,merge_grant_enabled) "
                    "VALUES (?,?,1)",
                    (feature_id, planner_shell_id),
                ).lastrowid
            )
            con.executemany(
                "INSERT INTO sprint_specs "
                "(sprint_id,document_id,bound_revision_sha256,approval_id) "
                "VALUES (?,?,?,?)",
                (
                    (
                        sprint_id,
                        approval["document_id"],
                        approval["revision_sha256"],
                        approval_id,
                    )
                    for approval_id, approval in approval_rows
                ),
            )
            con.executemany(
                "INSERT INTO sprint_participants "
                "(sprint_id,shell_id,role,harness,model,effort,route) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    (
                        sprint_id,
                        item["shell_id"],
                        item["role"],
                        item["harness"],
                        item["model"],
                        item["effort"],
                        item["route"],
                    )
                    for item in normalized
                ),
            )
            con.execute(
                "INSERT INTO sprint_events "
                "(sprint_id,event_type,actor_kind,actor_shell_id,payload) "
                "VALUES (?,'sprint.declared',?,?,?)",
                (
                    sprint_id,
                    "fnb" if caller["flavor"] == "admin" else "planner",
                    shell_id,
                    json.dumps(
                        {
                            "feature_id": feature_id,
                            "spec_approval_ids": approval_ids,
                            "participant_shell_ids": sorted(seen_shells),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return sprint_id

    def _sprint_get(self, path: str):
        shell_id = self._require_shell_auth()
        if shell_id is None:
            return
        parts = path.strip("/").split("/")
        con = db()
        try:
            if path == "/_sc/sprint/watcher-state":
                values = parse_qs(urlparse(self.path).query).get("sprint_id", [])
                if len(values) != 1:
                    raise ValueError("sprint_id query parameter is required once")
                try:
                    sprint_id = int(values[0])
                except ValueError as exc:
                    raise ValueError("sprint_id must be a positive integer") from exc
                state = sprint_pr_watcher.WatcherStateStore(con).for_sprint(sprint_id)
                return self._send(200, state)
            if len(parts) != 4:
                return self._send(404, {"error": "not found"})
            if parts[2] == "approvals":
                approvals = sprint_domain.SprintSpecApprovalStore(
                    con
                ).for_document(int(parts[3]))
                return self._send(200, {"approvals": approvals})
            sprint_id = int(parts[2])
            if parts[3] == "inbox":
                messages = sprint_message_delivery.SprintMessageStore(con).inbox(
                    sprint_id, shell_id
                )
                return self._send(
                    200,
                    {"sprint_id": sprint_id, "messages": [dict(row) for row in messages]},
                )
            store = sprint_close.SprintCloseStore(con)
            if parts[3] == "report":
                query = parse_qs(urlparse(self.path).query)
                limit = int(
                    query.get(
                        "limit", [str(sprint_close.DEFAULT_SECTION_LIMIT)]
                    )[0]
                )
                packet = store.compile_evidence_packet(
                    sprint_id, shell_id, section_limit=limit
                )
                return self._send(200, {"evidence_packet": packet})
            if parts[3] == "timeline":
                return self._send(200, store.timeline(sprint_id, shell_id))
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._sprint_error(exc)
        finally:
            con.close()

    def _sprint_post(self, path: str, body: dict):
        shell_id = self._require_shell_auth()
        if shell_id is None:
            return
        con = db()
        try:
            if path == "/_sc/sprint/qaqc":
                receipt = sprint_domain.SprintSpecApprovalStore(con).record(
                    self._sprint_integer(body, "document_id"),
                    shell_id,
                    verdict=body.get("verdict") or "",
                    findings_document_id=(
                        self._sprint_integer(body, "findings_document_id")
                        if body.get("findings_document_id") is not None
                        else None
                    ),
                )
                return self._send(201 if receipt.created else 200, {
                    "approval_id": receipt.approval_id,
                    "revision_sha256": receipt.revision_sha256,
                    "verdict": receipt.verdict,
                    "created": receipt.created,
                })
            if path == "/_sc/sprint/declare":
                sprint_id = self._declare_sprint(con, shell_id, body)
                return self._send(201, {"sprint_id": sprint_id})
            sprint_id = self._sprint_integer(body, "sprint_id")
            if path == "/_sc/sprint/plan-unit":
                planner_shell_id = self._sprint_planner_proxy(
                    con, sprint_id, shell_id
                )
                unit_id = sprint_domain.SprintWorkUnitStore(con).create(
                    sprint_id,
                    planner_shell_id,
                    assigned_shell_id=self._sprint_integer(
                        body, "assigned_shell_id"
                    ),
                    reviewer_shell_id=self._sprint_integer(
                        body, "reviewer_shell_id"
                    ),
                    title=body.get("title") or "",
                    expected_output=body.get("expected_output") or "",
                    task_ids=self._sprint_integer_list(body, "task_ids"),
                    planned_wave=int(body.get("planned_wave", 0)),
                    dependency_ids=(
                        self._sprint_integer_list(body, "dependency_ids")
                        if body.get("dependency_ids")
                        else ()
                    ),
                    output_kind=body.get("output_kind") or "code",
                )
                return self._send(201, {"work_unit_id": unit_id})
            if path == "/_sc/sprint/arm":
                planner_shell_id = self._sprint_planner_proxy(
                    con, sprint_id, shell_id
                )
                try:
                    wake_ids = sprint_domain.SprintLifecycleStore(con).arm(
                        sprint_id, planner_shell_id
                    )
                except sqlite3.IntegrityError as exc:
                    if "idx_sprints_single_armed" not in str(exc):
                        raise
                    raise sprint_domain.SprintInvariantError(
                        "another Sprint is already armed"
                    ) from exc
                return self._send(200, {"wake_ids": wake_ids})
            if path == "/_sc/sprint/replan-unit":
                planner_shell_id = self._sprint_planner_proxy(
                    con, sprint_id, shell_id
                )
                changed = sprint_domain.SprintWorkUnitStore(con).replan(
                    sprint_id,
                    self._sprint_integer(body, "work_unit_id"),
                    planner_shell_id,
                    assigned_shell_id=self._sprint_integer(
                        body, "assigned_shell_id"
                    ),
                    reviewer_shell_id=self._sprint_integer(
                        body, "reviewer_shell_id"
                    ),
                    planned_wave=int(body.get("planned_wave", 0)),
                    dependency_ids=(
                        self._sprint_integer_list(body, "dependency_ids")
                        if body.get("dependency_ids")
                        else ()
                    ),
                    output_kind=body.get("output_kind"),
                )
                return self._send(200, {"changed": changed})
            if path == "/_sc/sprint/complete-unit":
                wake_ids = sprint_domain.SprintWorkUnitStore(con).complete(
                    sprint_id,
                    self._sprint_integer(body, "work_unit_id"),
                    shell_id,
                    result=body.get("result") or "",
                )
                return self._send(200, {"wake_ids": wake_ids})
            if path == "/_sc/sprint/cancel-unit":
                planner_shell_id = self._sprint_planner_proxy(
                    con, sprint_id, shell_id
                )
                changed = sprint_domain.SprintWorkUnitStore(con).cancel(
                    sprint_id,
                    self._sprint_integer(body, "work_unit_id"),
                    planner_shell_id,
                    reason=body.get("reason") or "",
                )
                return self._send(200, {"changed": changed})
            if path == "/_sc/sprint/inbox-read":
                disposition = sprint_message_delivery.SprintMessageStore(
                    con
                ).mark_read(
                    self._sprint_integer(body, "message_id"),
                    shell_id,
                    sprint_id=sprint_id,
                )
                return self._send(200, {"disposition": disposition})
            if path == "/_sc/sprint/inbox-decline":
                message_id = sprint_message_delivery.SprintMessageStore(con).decline(
                    self._sprint_integer(body, "message_id"),
                    shell_id,
                    body.get("reason") or "",
                    sprint_id=sprint_id,
                )
                return self._send(200, {"result_message_id": message_id})
            if path == "/_sc/sprint/send":
                receipt = sprint_message_delivery.SprintMessageStore(con).relay(
                    sprint_id,
                    from_shell_id=shell_id,
                    to_shortname=body.get("to") or "",
                    body=body.get("body") or "",
                    idempotency_key=body.get("idempotency_key") or "",
                )
                return self._send(201 if receipt.message_created else 200, {
                    "message_id": receipt.message_id,
                    "wake_id": receipt.wake_id,
                    "message_created": receipt.message_created,
                    "wake_state": receipt.wake_state,
                    "conversation_id": receipt.conversation_id,
                })
            if path == "/_sc/sprint/register-pr":
                receipt = sprint_pr_watcher.SprintPRWatcher(
                    con, repo_root=REPO_ROOT
                ).register(
                    sprint_id,
                    owner_shell_id=shell_id,
                    repository=body.get("repository") or "",
                    pr_number=self._sprint_integer(body, "pr_number"),
                    work_unit_ids=self._sprint_integer_list(
                        body, "work_unit_ids"
                    ),
                )
                return self._send(201 if receipt.created else 200, {
                    "registered_pr_id": receipt.registered_pr_id,
                    "created": receipt.created,
                })
            if path == "/_sc/sprint/pause":
                receipt = sprint_recovery.SprintRecoveryCoordinator(
                    con, repo_root=REPO_ROOT
                ).pause(
                    sprint_id,
                    self._sprint_actor(con, sprint_id, shell_id),
                    reason=body.get("reason") or "",
                )
                return self._send(200, {
                    "changed": receipt.changed,
                    "report_id": receipt.report_id,
                    "interrupt_run_ids": list(receipt.interrupt_run_ids),
                    "notification_conversation_ids": list(
                        receipt.notification_conversation_ids
                    ),
                })
            if path == "/_sc/sprint/resume":
                receipt = sprint_recovery.SprintRecoveryCoordinator(
                    con, repo_root=REPO_ROOT
                ).resume(
                    sprint_id,
                    self._sprint_actor(con, sprint_id, shell_id),
                    reason=body.get("reason"),
                )
                return self._send(200, {
                    "changed": receipt.changed,
                    "dispatched_wake_ids": list(receipt.dispatched_wake_ids),
                    "requeued_wake_ids": list(receipt.requeued_wake_ids),
                    "projected_work_unit_ids": list(
                        receipt.projected_work_unit_ids
                    ),
                    "resolved_review_message_ids": list(
                        receipt.resolved_review_message_ids
                    ),
                    "spec_drift_document_ids": list(
                        receipt.spec_drift_document_ids
                    ),
                    "anomalies": list(receipt.anomalies),
                })
            if path == "/_sc/sprint/complete":
                report_body = body.get("final_report")
                report_key = body.get("idempotency_key")
                if (report_body is None) != (report_key is None):
                    raise ValueError(
                        "final_report and idempotency_key must be provided together"
                    )
                report = None
                if report_body is not None:
                    report = sprint_close.SprintCloseStore(con).record_final_report(
                        sprint_id,
                        shell_id,
                        body=report_body,
                        idempotency_key=report_key,
                    )
                changed = sprint_domain.SprintLifecycleStore(con).transition(
                    sprint_id,
                    "completed",
                    self._sprint_actor(con, sprint_id, shell_id),
                    reason=body.get("reason"),
                    terminal_outcome=body.get("terminal_outcome"),
                )
                return self._send(200, {
                    "changed": changed,
                    "report_id": report.report_id if report else None,
                    "report_created": report.created if report else False,
                })
            if path == "/_sc/sprint/abort":
                receipt = sprint_recovery.SprintRecoveryCoordinator(
                    con, repo_root=REPO_ROOT
                ).abort(
                    sprint_id,
                    self._sprint_actor(con, sprint_id, shell_id),
                    reason=body.get("reason") or "",
                    terminal_outcome=body.get("terminal_outcome") or "aborted",
                )
                return self._send(200, {
                    "changed": receipt.changed,
                    "report_id": receipt.report_id,
                    "interrupt_run_ids": list(receipt.interrupt_run_ids),
                    "notification_conversation_ids": list(
                        receipt.notification_conversation_ids
                    ),
                })
            if path == "/_sc/sprint/review-request":
                receipt = sprint_review_loop.SprintReviewLoopStore(
                    con, repo_root=REPO_ROOT
                ).request_review(
                    sprint_id,
                    self._sprint_integer(body, "registered_pr_id"),
                    shell_id,
                    readiness=body.get("readiness") or "",
                    idempotency_key=body.get("idempotency_key") or "",
                )
                return self._send(201 if receipt.created else 200, {
                    "work_unit_id": receipt.work_unit_id,
                    "message_id": receipt.message_id,
                    "wake_id": receipt.wake_id,
                    "created": receipt.created,
                })
            if path == "/_sc/sprint/review-record":
                receipt = sprint_review_loop.SprintReviewLoopStore(
                    con, repo_root=REPO_ROOT
                ).record_review(
                    sprint_id,
                    self._sprint_integer(body, "registered_pr_id"),
                    shell_id,
                    verdict=body.get("verdict") or "",
                    body=body.get("body") or "",
                    idempotency_key=body.get("idempotency_key") or "",
                )
                return self._send(201 if receipt.created else 200, {
                    "work_unit_id": receipt.work_unit_id,
                    "conversation_id": receipt.conversation_id,
                    "message_id": receipt.message_id,
                    "wake_id": receipt.wake_id,
                    "disposition": receipt.disposition,
                    "created": receipt.created,
                })
            if path == "/_sc/sprint/merge-authorize":
                authorization = sprint_review_loop.SprintReviewLoopStore(
                    con, repo_root=REPO_ROOT
                ).authorize_merge(
                    sprint_id,
                    self._sprint_integer(body, "registered_pr_id"),
                    shell_id,
                )
                return self._send(200, {
                    "registered_pr_id": authorization.registered_pr_id,
                    "repository": authorization.repository,
                    "pr_number": authorization.pr_number,
                    "head_sha": authorization.head_sha,
                })
            if path == "/_sc/sprint/dispatch":
                self._require_sprint_planner(con, sprint_id, shell_id)
                released = sprint_domain.SprintWorkUnitStore(con).dispatch_ready(
                    sprint_id
                )
                return self._send(200, {"wake_ids": released})
            if path == "/_sc/sprint/monitor":
                self._require_sprint_planner(con, sprint_id, shell_id)
                return self._send(200, sprint_monitor_response(con, sprint_id))
            if path == "/_sc/sprint/conformance":
                findings = body.get("findings")
                if not isinstance(findings, list):
                    raise ValueError("findings must be a JSON array")
                receipt = sprint_close.SprintCloseStore(con).record_conformance(
                    sprint_id,
                    shell_id,
                    body=body.get("body") or "",
                    findings=findings,
                    final_report=body.get("final_report") or "",
                    reason=body.get("reason") or "",
                    terminal_outcome=body.get("terminal_outcome") or "",
                    idempotency_key=body.get("idempotency_key") or "",
                )
                return self._send(201 if receipt.created else 200, {
                    "report_id": receipt.report_id,
                    "followup_ids": list(receipt.followup_ids),
                    "final_report_id": receipt.final_report_id,
                    "planner_message_id": receipt.planner_message_id,
                    "planner_wake_id": receipt.planner_wake_id,
                    "completed": receipt.completed,
                    "created": receipt.created,
                })
            if path == "/_sc/sprint/followup-disposition":
                changed = sprint_close.SprintCloseStore(con).disposition_followup(
                    sprint_id,
                    self._sprint_integer(body, "followup_id"),
                    shell_id,
                    disposition=body.get("disposition") or "",
                    resolution=body.get("resolution"),
                )
                return self._send(200, {"changed": changed})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._sprint_error(exc)
        finally:
            con.close()

    def _pr_post(self, path: str, body: dict):
        """Authenticated shell-owned PR subscription surface."""
        shell_id = self._require_shell_auth()
        if shell_id is None:
            return
        con = db()
        try:
            if path != "/_sc/pr/subscribe":
                return self._send(404, {"error": "not found"})
            try:
                pr_number = int(body.get("pr_number"))
            except (TypeError, ValueError) as exc:
                raise ValueError("pr_number must be a positive integer") from exc
            receipt = sprint_pr_watcher.SprintPRWatcher(
                con, repo_root=REPO_ROOT
            ).subscribe(
                owner_shell_id=shell_id,
                repository=body.get("repository") or "",
                pr_number=pr_number,
            )
            return self._send(201 if receipt.created else 200, {
                "subscription_id": receipt.subscription_id,
                "created": receipt.created,
            })
        except Exception as exc:
            return self._sprint_error(exc)
        finally:
            con.close()

    # -- authenticated shell CLI catalogue reads --

    def _shell_catalog_get(self, path: str):
        if self._require_shell_auth() is None:
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        con = db()
        try:
            if path == "/_sc/skills":
                if query:
                    return self._send(400, {"error": "skills takes no filters"})
                return self._send(200, get_cli_skills(con))
            if path == "/_sc/model-routes":
                unknown = set(query) - {"harness", "selector"}
                repeated = [name for name, values in query.items() if len(values) != 1]
                empty = [name for name, values in query.items() if not values[0]]
                if unknown or repeated or empty:
                    return self._send(400, {
                        "error": "model route filters must be one non-empty "
                                 "harness and/or selector",
                    })
                return self._send(200, get_model_routes(
                    con,
                    harness=(query.get("harness") or [None])[0],
                    selector=(query.get("selector") or [None])[0],
                ))
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._fail(exc)
        finally:
            con.close()

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
                # default to every open flag in the fleet. Resolved history is
                # available only for one feature so this surface cannot become
                # an unbounded fleet-wide history dump.
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                if not q:
                    where = "COALESCE(f.resolved,0)=0"
                    params = ()
                else:
                    unknown = set(q) - {"feature", "resolved"}
                    repeated = [name for name, values in q.items() if len(values) != 1]
                    if unknown or repeated or q.get("resolved") != ["1"]:
                        return self._send(400, {
                            "error": "flag history needs one feature=<id> and resolved=1",
                        })
                    raw_feature = (q.get("feature") or [""])[0]
                    if not raw_feature.isdigit() or int(raw_feature) < 1:
                        return self._send(400, {
                            "error": "flag history needs one positive feature=<id>",
                        })
                    where = "f.feature_id=? AND COALESCE(f.resolved,0)=1"
                    params = (int(raw_feature),)
                fs = rows(con.execute(
                    "SELECT f.flag_id, f.display_name, f.priority, f.description, "
                    "f.feature_id, f.created_date, f.resolved, f.resolved_date, "
                    "f.resolution_notes, s.shortname AS owner, r.title AS feature_title "
                    "FROM flags f "
                    "LEFT JOIN shells s ON s.shell_id=f.shell_id "
                    "LEFT JOIN roadmap r ON r.feature_id=f.feature_id "
                    f"WHERE {where} AND COALESCE(f.is_deleted,0)=0 "
                    "ORDER BY f.created_date, f.flag_id", params))
                return self._send(200, {"flags": fs})

            if len(parts) == 4 and parts[2] == "flags":
                # One flag by id, RESOLVED ONES INCLUDED — the list above is
                # open-only, so it cannot answer "what am I about to close?"
                # (#149). `sc mem flag close` reads this before it writes, and
                # a row that is already resolved carries the notes the closer
                # would otherwise overwrite.
                fid = int(parts[3])
                r = con.execute(
                    "SELECT f.flag_id, f.display_name, f.priority, f.description, "
                    "f.feature_id, f.created_date, f.resolved, f.resolved_date, "
                    "f.resolution_notes, "
                    "(SELECT s.shortname FROM shells s WHERE s.shell_id=f.shell_id) "
                    " AS owner, "
                    "(SELECT title FROM roadmap WHERE feature_id=f.feature_id) "
                    " AS feature_title "
                    "FROM flags f WHERE f.flag_id=? "
                    "AND COALESCE(f.is_deleted,0)=0", (fid,)).fetchone()
                if r is None:
                    return self._send(404, {"error": "no such flag"})
                return self._send(200, {"flag": dict(r)})

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
                        "AND m.kind IN ('shell','task','result') "
                        "ORDER BY m.created_at DESC LIMIT 50", (sid,)))
                    return self._send(200, {"messages": msgs, "direction": "sent"})
                msgs = rows(con.execute(
                    "SELECT message_id, from_shell_id, kind, body, created_at, read_at "
                    "FROM shell_messages WHERE to_shell_id=? "
                    "AND kind IN ('shell','task','result') "
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
                try:
                    con.execute("UPDATE shells SET current_state=? WHERE shell_id=?",
                                ((body.get("body") or ""), sid))
                except db_driver.IntegrityError as e:   # the 300-char cap
                    con.rollback()
                    return self._send(400, {"error": str(e)})
                con.commit()
                return self._send(200, {"ok": True})

            # The curation stamp (migration 0100). Unconditional by design: a
            # sweep that finds a clean set and retires NOTHING still clears the
            # counter, or the advisory would stand forever on a shell that has
            # done the work. This is the case a MAX(retired_at) signal would
            # have shipped broken.
            if path == "/_sc/mem/lns/curated":
                con.execute(
                    "UPDATE shells SET lns_curated_at=datetime('now') WHERE shell_id=?",
                    (sid,))
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
                # L&S supersession: the missing verb. Five of one shell's twenty
                # entries NAMED the entry they duplicated and added anyway —
                # `lns` offered exactly one verb, so the reconciliation the shell
                # had already done had nowhere to land. Retire FIRST, then
                # insert: a supersede at 20/20 must succeed (the freed slot is
                # the point), and both halves ride one transaction so a rejected
                # insert can never leave the old entries retired for nothing.
                retired: list[int] = []
                if kind == "lns" and body.get("supersedes"):
                    try:
                        retired = [int(i) for i in body["supersedes"]]
                    except (TypeError, ValueError):
                        return self._send(400, {"error": "supersedes must be entry ids"})
                    for eid in retired:
                        if not con.execute(
                                "SELECT 1 FROM shell_identity_entries "
                                "WHERE entry_id=? AND shell_id=? AND kind='lns' "
                                "AND is_deleted=0 AND retired_at IS NULL",
                                (eid, sid)).fetchone():
                            return self._send(404, {
                                "error": f"entry #{eid} is not one of your active "
                                         "L&S entries — `sc mem get lns` lists them"})
                    con.execute(
                        "UPDATE shell_identity_entries SET retired_at=datetime('now') "
                        "WHERE entry_id IN (%s)" % ",".join("?" * len(retired)),
                        tuple(retired))
                try:
                    cur = con.execute(
                        "INSERT INTO shell_identity_entries "
                        "(shell_id, kind, body, entry_date, source_tag) VALUES (?, ?, ?, ?, ?)",
                        (sid, kind, b,
                         body.get("entry_date") or None,
                         body.get("source_tag") or None))
                except db_driver.IntegrityError as e:
                    # A cap trigger (count or length) fired. That is a client
                    # error carrying its own remedy, not a server fault — a 500
                    # would read as "the engine broke" and bury the routing
                    # instruction the message exists to deliver.
                    con.rollback()
                    return self._send(400, {"error": str(e)})
                con.commit()
                return self._send(201, {"entry_id": cur.lastrowid,
                                        "retired": retired})

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
                unknown = sorted(
                    set(body)
                    - {"to", "to_shell_id", "body", "kind", "dedupe_key"}
                )
                if unknown:
                    return self._send(400, {
                        "error": "unknown message field(s): " + ", ".join(unknown)
                    })
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
                dk = (body.get("dedupe_key") or "").strip() or None
                # Idempotent send (#333): a client timeout after the server-side
                # write left the sender unable to tell delivered from lost, and
                # blind resends duplicated fleet-wide. The client stamps each
                # send invocation with a dedupe_key; a resend of the same key
                # returns the original row instead of inserting a twin. The
                # unique index (from_shell_id, dedupe_key) backstops the
                # check-then-insert race.
                if dk is not None:
                    r = con.execute(
                        "SELECT message_id FROM shell_messages "
                        "WHERE from_shell_id=? AND dedupe_key=?", (sid, dk)).fetchone()
                    if r is not None:
                        return self._send(200, {"message_id": r[0], "duplicate": True})
                try:
                    cur = con.execute(
                        "INSERT INTO shell_messages "
                        "(from_shell_id,to_shell_id,body,kind,dedupe_key) "
                        "VALUES (?,?,?,?,?)",
                        (sid, int(to_sid), msg, kind, dk))
                    message_id = cur.lastrowid
                    con.commit()
                except db_driver.IntegrityError:
                    con.rollback()
                    r = con.execute(
                        "SELECT message_id FROM shell_messages "
                        "WHERE from_shell_id=? AND dedupe_key=?", (sid, dk)).fetchone()
                    if r is None:
                        raise
                    return self._send(200, {"message_id": r[0], "duplicate": True})
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
                # Append to the description in ONE statement (#288 follow-on).
                # `description` replaces the whole body, so growing a long-lived
                # tracker flag meant fetch → concatenate → rewrite in the
                # client, and two shells doing that concurrently lose one
                # edit. Server-side `||` has no read to lose.
                add = body.pop("append_description", None)
                if add:
                    con.execute(
                        "UPDATE flags SET description = "
                        "COALESCE(description,'') || ? WHERE flag_id=?",
                        (add, fid))
                    con.commit()
                    if not any(c in body for c in FLAG_EDITABLE | {"resolved_date"}):
                        return self._send(200, {"ok": True})
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
                con.commit()
                return self._send(200, {
                    "ok": True,
                    "serialize": serialize_doc_write(),
                })

            # PATCH /_sc/mem/docs/{id}
            if len(parts) == 4 and parts[2] == "docs":
                did = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM documents WHERE document_id=?",
                        (did,)).fetchone():
                    return self._send(404, {"error": "no such document"})
                ok, err = patch_document(con, did, body, commit=False)
                if not ok:
                    return self._send(400, {"ok": ok, "error": err})
                con.commit()
                return self._send(200, {
                    "ok": ok,
                    "serialize": serialize_doc_write(),
                })

            # PATCH /_sc/mem/messages/{id}/read
            if len(parts) == 5 and parts[2] == "messages" and parts[4] == "read":
                mid = int(parts[3])
                if not con.execute(
                        "SELECT 1 FROM shell_messages WHERE message_id=? "
                        "AND to_shell_id=? "
                        "AND kind IN ('shell','task','result')",
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

    def _serve_vendor(self, path: str):
        """GET/HEAD a vendored asset, resolved against the tree as it is NOW.

        Headers match the shell files': `no-cache` plus a content-derived
        ETag, and the same 304. The tag is computed over raw bytes, which is
        byte-identical to the text path for the current vendor set, so nothing
        re-downloads on rollout.
        """
        hit, ctype_or_reason = _resolve_vendor(unquote(path[len("/vendor/"):]))
        if hit is None:
            # Name the gate. The old two 404 shapes (JSON route-miss vs
            # `not built`) were what made the outage diagnosable at all, and a
            # per-request resolver collapses that distinction — so replace it
            # deliberately rather than losing it. Loopback-only surface: the
            # reason costs nothing and is half of what this route is for.
            return self._send(404, ctype_or_reason, "text/plain")
        data = hit.read_bytes()
        headers = {
            "Cache-Control": "no-cache",
            "ETag": '"%s"' % hashlib.sha256(data).hexdigest()[:32],
        }
        if self._if_none_match(headers["ETag"]):
            return self._not_modified(headers)
        return self._send(200, data, ctype_or_reason, headers=headers)

    def do_HEAD(self):
        """Vendored assets answer HEAD; everything else keeps the 405.

        The app shell probes its own `<script src="/vendor/…">` tags with HEAD
        to tell "not executed yet" from "the floor cannot serve this build"
        (spec #48). Against a server that 405s the method, every healthy
        script reads as a broken floor — the probe would manufacture exactly
        the dishonest report it exists to replace. HEAD on the API is not a
        contract this server offers, so the narrow route is the whole fix.
        """
        path = urlparse(self.path).path
        if not path.startswith("/vendor/"):
            # Same body the shim's send_error would have produced, sent the
            # way HEAD requires: headers only, no bytes after them.
            return self._send(405, {"error": "method not allowed"})
        return self._serve_vendor(path)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/vendor/"):
            return self._serve_vendor(path)
        if path in _STATIC:
            fname, ctype = _STATIC[path]
            f = UI_DIR / fname
            if not f.exists():
                return self._send(404, "not built", "text/plain")
            text = f.read_text()
            # Freshness (spec #43 U3): `no-cache` means revalidate, not
            # don't-store — every navigation asks, and the ETag answers 304
            # from the client's own copy when nothing changed. Without this a
            # heuristically-cached app.js survived an engine update until the
            # operator hard-refreshed, which is what issue 3 actually was.
            # Content-derived, so a rebuild that produces identical bytes
            # still revalidates cheaply.
            headers = {
                "Cache-Control": "no-cache",
                "ETag": '"%s"' % hashlib.sha256(text.encode()).hexdigest()[:32],
            }
            # Restrictive CSP on the app shell (spec #20 Security): vendored
            # scripts/styles + same-origin connections only, no inline script.
            if fname == "index.html":
                headers["Content-Security-Policy"] = _CSP
            if self._if_none_match(headers["ETag"]):
                return self._not_modified(headers)
            return self._send(200, text, ctype, headers=headers)
        if path in ("/_sc/model-routes", "/_sc/skills"):
            return self._shell_catalog_get(path)
        if path.startswith("/_sc/mem/"):
            return self._mem_get(path)
        if path.startswith("/_sc/sprint/"):
            return self._sprint_get(path)
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
                return self._send(200, health_payload())
            if path == "/api/shells":
                # repo_root rides along for the browser chat rail: admin
                # shells are CLI-only there, and the notice shows the exact
                # `cd <repo_root> && make dos-e s=<shortname>` to run instead.
                return self._send(200, {"shells": get_shells(con),
                                        "repo_root": str(REPO_ROOT)})
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
            if path == "/api/sprints":
                if not self._require_browser_operator(con):
                    return None
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                try:
                    result = sprint_board.SprintBoardProjection(con).list_sprints(
                        lifecycle=q.get("lifecycle", [None])[0],
                        limit=sprint_board.parse_limit(q.get("limit", [None])[0]),
                        cursor=q.get("cursor", [None])[0],
                    )
                except Exception as exc:
                    return self._sprint_board_error(exc)
                return self._send(200, result)
            if path.startswith("/api/sprints/"):
                if not self._require_browser_operator(con):
                    return None
                parts = path.strip("/").split("/")
                try:
                    if len(parts) not in {3, 4}:
                        raise sprint_board.ProjectionError(
                            404, "not_found", "resource not found"
                        )
                    sprint_id = int(parts[2])
                    if sprint_id <= 0:
                        raise ValueError
                except ValueError:
                    return self._send(404, {"error": {
                        "code": "sprint_not_found",
                        "message": "Sprint not found",
                        "details": {},
                    }})
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                projection = sprint_board.SprintBoardProjection(con)
                try:
                    if len(parts) == 3:
                        result = projection.board(sprint_id)
                    elif parts[3] == "events":
                        result = projection.events(
                            sprint_id,
                            limit=sprint_board.parse_limit(q.get("limit", [None])[0]),
                            cursor=q.get("cursor", [None])[0],
                            work_unit_id=sprint_board.parse_work_unit_id(
                                q.get("work_unit_id", [None])[0]
                            ),
                        )
                    elif parts[3] == "summaries":
                        result = projection.summaries(
                            sprint_id,
                            limit=sprint_board.parse_limit(q.get("limit", [None])[0]),
                            cursor=q.get("cursor", [None])[0],
                            work_unit_id=sprint_board.parse_work_unit_id(
                                q.get("work_unit_id", [None])[0]
                            ),
                        )
                    else:
                        raise sprint_board.ProjectionError(
                            404, "not_found", "resource not found"
                        )
                except Exception as exc:
                    return self._sprint_board_error(exc)
                return self._send(200, result)
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
            if path == "/api/analytics/quota":
                # Arrival at #analytics-quota. Probes only when this process's
                # last probe ATTEMPT is older than the TTL — never at boot,
                # never on a timer, never from the Token Analytics section.
                return self._send(200, get_analytics_quota(con))
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
        if path.startswith("/_sc/pr/"):
            return self._pr_post(path, self._body())
        if path.startswith("/_sc/sprint/"):
            return self._sprint_post(path, self._body())
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
                if not body.get("name") or "flavor" not in body:
                    return self._send(400, {"error": "name and shell type required"})
                flavor = body.get("flavor") or None
                try:
                    sid = shell_factory.create_shell(
                        con, flavor=flavor, name=body["name"],
                        shortname=body.get("shortname"), partner=body.get("partner"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                con.commit()
                sn = con.execute(
                    "SELECT shortname FROM shells WHERE shell_id=?", (sid,)).fetchone()[0]
                return self._send(201, {"shell_id": sid, "shortname": sn})
            if path == "/api/flavor-defaults":
                ok, err = set_flavor_default(con, self._body())
                if ok:
                    return self._send(200, {"ok": True})
                if err and err.startswith("invalid_model_route:"):
                    return self._send(422, {"error": {
                        "code": "invalid_model_route",
                        "message": err.split(":", 1)[1].strip()}})
                return self._send(400, {"error": err})
            if path == "/api/analytics/sweep":
                # GUI Analytics tab load — incremental, so steady-state is
                # cheap; sweep opens its own connection.
                return self._send(200, analytics.sweep(quiet=True))
            if path == "/api/analytics/quota/probe":
                # The refresh button — always re-probes, TTL or not.
                return self._send(200, get_analytics_quota(con, force=True))
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
                return self._send(410, {
                    "error": {
                        "code": "publish_retired",
                        "message": "Git publication of generated artifacts is "
                        "retired; use /api/snapshot to save locally",
                        "details": {},
                    }
                })
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
            if path.startswith("/api/sprints/") and path.count("/") == 3:
                if not self._require_browser_operator(con):
                    return None
                if not self._require_browser_mutation_origin():
                    return None
                try:
                    sprint_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    sprint_id = 0
                if sprint_id <= 0:
                    return self._send(404, {"error": {
                        "code": "sprint_not_found",
                        "message": "Sprint not found",
                        "details": {},
                    }})
                if not isinstance(body, dict):
                    return self._send(422, {"error": {
                        "code": "validation_error",
                        "message": "request body must be a JSON object",
                        "details": {},
                    }})
                unknown = sorted(set(body) - {"lifecycle", "reason"})
                if unknown:
                    return self._send(422, {"error": {
                        "code": "validation_error",
                        "message": "unknown field(s): " + ", ".join(unknown),
                        "details": {"fields": unknown},
                    }})
                target = body.get("lifecycle")
                if target not in {"paused", "armed", "aborted"}:
                    return self._send(422, {"error": {
                        "code": "validation_error",
                        "message": "lifecycle must be paused, armed, or aborted",
                        "details": {"field": "lifecycle"},
                    }})
                reason = body.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    return self._send(422, {"error": {
                        "code": "validation_error",
                        "message": "reason must be a nonblank string",
                        "details": {"field": "reason"},
                    }})
                reason = reason.strip()
                if len(reason) > 2000:
                    return self._send(422, {"error": {
                        "code": "validation_error",
                        "message": "reason must be at most 2000 characters",
                        "details": {"field": "reason"},
                    }})
                try:
                    coordinator = sprint_recovery.SprintRecoveryCoordinator(
                        con, repo_root=REPO_ROOT
                    )
                    actor = sprint_domain.LifecycleActor("fnb")
                    if target == "paused":
                        changed = coordinator.pause(
                            sprint_id, actor, reason=reason
                        ).changed
                    elif target == "armed":
                        changed = coordinator.resume(
                            sprint_id, actor, reason=reason
                        ).changed
                    else:
                        changed = coordinator.abort(
                            sprint_id, actor, reason=reason
                        ).changed
                    sprint = sprint_board.SprintBoardProjection(con).board(sprint_id)["sprint"]
                except Exception as exc:
                    return self._sprint_board_mutation_error(exc)
                return self._send(200, {"changed": changed, "sprint": sprint})
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
        # grant toggles:
        #   PUT /api/flavors/{flavor}/skills/{skill_id}  {granted: bool}
        #   PUT /api/shells/{id}/skills/{skill_id}        {granted: bool}
        # vm block:     PUT /api/vm  {vm: {...}}  (persists to instance.json)
        # ts block:     PUT /api/ts  {ts: {...}}  (persists to instance.json)
        # pm2 block:    PUT /api/pm2  {pm2: {...}}  (persists to instance.json)
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        con = db()
        try:
            if len(parts) == 5 and parts[1] == "flavors" and parts[3] == "skills":
                ok, err = set_flavor_grant(
                    con, parts[2], int(parts[4]),
                    bool(self._body().get("granted")))
                if ok:
                    try:
                        skill_projection.reconcile_flavors(con, [parts[2]])
                    except skill_projection.ProjectionError as exc:
                        error = skill_projection.partial_failure_message(
                            f"flavor {parts[2]} skill toggle", exc
                        )
                        return self._send(500, {
                            "ok": False, "committed": True, "error": error,
                        })
                return self._send(200 if ok else 404,
                                  {"ok": ok, "error": err})
            if len(parts) == 5 and parts[1] == "shells" and parts[3] == "skills":
                shell_id = int(parts[2])
                ok, err = set_grant(
                    con, shell_id, int(parts[4]),
                    bool(self._body().get("granted")))
                if ok:
                    try:
                        skill_projection.reconcile_assignment_targets(
                            con, [shell_id]
                        )
                    except skill_projection.ProjectionError as exc:
                        error = skill_projection.partial_failure_message(
                            f"shell {shell_id} skill toggle", exc
                        )
                        return self._send(500, {
                            "ok": False, "committed": True, "error": error,
                        })
                status = 200 if ok else (
                    409 if err and "edit the flavor pack" in err else 404)
                return self._send(status, {"ok": ok, "error": err})
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
# asyncio transport integration
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
    (status, [(header, value)], body bytes). Focused retained routes go to
    their own modules; everything else runs through the shimmed Handler."""
    parsed = urlparse(path)
    if (
        parsed.path.startswith("/api/review-targets/")
        or parsed.path.startswith("/api/review-observations/")
        or (
            parsed.path.startswith("/api/conversations/")
            and parsed.path.endswith(("/review-targets", "/review-observations"))
        )
    ):
        return review_routes.handle(method, path, headers_raw, body)
    if parsed.path.startswith("/api/conversations"):
        return conversation_routes.handle(method, path, headers_raw, body)
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


# Filesystem artifacts a container RUNTIME creates inside the container — not
# configuration this process, its operator, or a stray shell can assert. Docker
# writes /.dockerenv; podman and the other OCI runtimes write /run/.containerenv.
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
_PID1_CGROUP = Path("/proc/1/cgroup")


def in_container() -> bool:
    """Positive evidence that this process runs inside a container, observed
    rather than asserted (spec #26, conformance finding SC-149).

    What it checks, in order of trustworthiness: a runtime-created marker file
    (`/.dockerenv`, `/run/.containerenv`), then PID 1's cgroup path naming a
    container runtime — the cgroup-v1-shaped fallback for runtimes that write
    no marker. Both are artifacts of the runtime that built the sandbox.

    What it ESTABLISHES: a container filesystem and cgroup context — this
    process runs inside something a container runtime built. That is NOT a
    distinct network namespace. `--network=host` shares the host's namespace
    and passes every check here, so `0.0.0.0` under this exemption is not
    guaranteed to be the container's own wildcard.

    The loopback boundary therefore rests on an operator PRECONDITION, not on
    a verified property: that the engine was launched by `./sc launch`, which
    publishes `-p 127.0.0.1:PORT:PORT` (`sc:1206-1207`) and does not pass
    `--network=host`. Nothing observable from in here can confirm it — the
    publish mapping is host-side and invisible without the docker socket — and
    a stated assumption is not a verified one. Deliberately do not add a
    namespace probe to close that gap: a check that fails open would be worse
    than an honest narrow one, and claiming more than is verified is the
    defect this replaced (SC-149, then SC-154 one layer up).

    What the check does buy: the exemption now costs a deliberate launch
    outside `./sc launch`, where the `SC_SANDBOX` check it replaces cost an
    `export`.
    """
    if any(os.path.exists(m) for m in _CONTAINER_MARKERS):
        return True
    try:
        cgroup = _PID1_CGROUP.read_text()
    except OSError:
        return False
    return any(tag in cgroup for tag in
               ("/docker/", "/docker-", "containerd", "/lxc/", "kubepods"))


def require_loopback_bind(bind: str) -> None:
    """Spec #26 Failure Modes: a non-loopback bind refuses to start, because
    the Interface hands a browser session to anything that can present an
    allowed `Host` and a same-origin `Origin` — headers a REMOTE client
    chooses freely. On a host, a non-loopback bind therefore turns automatic
    minting into remote authority, and no other fence stands behind it.

    The sandbox is the one legitimate non-loopback bind: `./sc launch` sets
    SC_BIND=0.0.0.0 (`sc:1198`) so docker can publish the port, and the
    boundary is the `-p 127.0.0.1:PORT:PORT` mapping (`sc:1206-1207`) —
    loopback-only on the host provided that is how the container was launched,
    which is a precondition this process cannot check (see `in_container()`).
    Refusing 0.0.0.0 unconditionally would make the sandbox unlaunchable while
    removing no real exposure.

    That exception is gated on `in_container()`, NOT on SC_SANDBOX. The
    original amendment keyed it on the env var and conformance finding SC-149
    was right to reject it: an environment variable is a CLAIM that a boundary
    exists, and setting `SC_SANDBOX=1` does not create a publish mapping, so
    `SC_SANDBOX=1 SC_BIND=0.0.0.0` on a bare host opened a listener the spec
    asserted was fenced. Where the boundary cannot be positively observed, the
    refusal applies — see `in_container()` for exactly how far the observation
    reaches.
    """
    host = (bind or "").strip().strip("[]")
    if host.lower() == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    if in_container():
        return
    sys.exit(
        f"server: refusing to start — SC_BIND={bind!r} is not a loopback "
        "address and this process is not in a container (spec #26). The "
        "Interface mints a browser session for any caller that can set Host "
        "and Origin, so a non-loopback bind would expose it to the network. "
        "Unset SC_BIND or set it to 127.0.0.1. SC_SANDBOX no longer grants "
        "this exception — it is a claim, not a boundary. Remote access needs "
        "a separately authenticated boundary (e.g. a tailnet), not a wider "
        "bind."
    )


def start_runtime_services() -> None:
    """Start commit-woken conversations and Sprint/engine services."""
    broker = conversation_broker.start_service(
        DB_PATH,
        launch_preparer=conversation_launch.ConversationLaunchPreparer(DB_PATH),
    )
    conversation_reaper.start_service(DB_PATH, native_interrupt=broker.interrupt)
    runtime = sprint_runtime.start_service(DB_PATH)
    if not runtime.wait_ready():
        runtime.stop()
        raise RuntimeError(
            "Sprint runtime did not complete its first successful cycle"
        )
    if not runtime.is_alive():
        runtime.stop()
        raise RuntimeError("Sprint runtime died during startup")
    sprint_pr_watcher.start_service(DB_PATH, repo_root=REPO_ROOT)


def sprint_monitor_response(
    con: sqlite3.Connection,
    sprint_id: int,
) -> dict[str, object]:
    """Run one idempotent pickup/liveness evaluation without wake delivery."""
    requeued = sprint_domain.SprintLifecycleStore(con).reconcile_unread_pickup(
        sprint_id,
        trigger="monitor",
    )
    outcomes = sprint_liveness.SprintLivenessMonitor(con).evaluate(sprint_id)
    return {
        "outcomes": [
            {
                "message_id": outcome.message_id,
                "action": outcome.action,
                "silence_episode": outcome.silence_episode,
            }
            for outcome in outcomes
        ],
        "pickup": sprint_board.pickup_projection(
            con,
            sprint_id,
            requeued_wake_ids=requeued,
            include_exhausted=False,
        ),
        "runtime": sprint_runtime.runtime_status(con),
    }


def main(argv):
    port = None
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if port is None:
        port = ports_mod.resolve().get("port", 8800)
    # The app shell's socket sources are port-exact (see `_csp`), so bind the
    # policy to the port actually being served before any request can render it.
    global _CSP
    _CSP = _csp(port)
    if not DB_PATH.exists():
        sys.exit(f"server: no DB at {DB_PATH} — run `./sc rebuild` first.")
    require_current_schema(DB_PATH)
    # Provision API keys at startup: every shell needs an api_key to reach this
    # server's token-scoped routes, but shells created before migration 0027 (or
    # on a fork that never ran the one-off backfill) come up NULL-keyed and would
    # silently fall back to direct-DB. The running API owns key provisioning — so
    # ensure it here, idempotently, on every boot. Reuses the same minting the
    # auth path resolves against; a `make launch/restart` thus self-heals keys
    # (no separate `./sc update` step). New shells are still keyed at creation.
    backfill_shell_api_keys.backfill(str(DB_PATH))
    # Runtime Admin credentials (spec #30 req 11, #516): one owner-only
    # artifact per Admin shell, so a host Admin seat without injected
    # SC_API_BASE/SC_API_TOKEN can still reach this API through `sc mem`
    # discovery. Rewritten on every boot, right after the key backfill — an
    # api_key rotation is picked up here. Lives under the gitignored,
    # never-snapshotted .super-coder/run/.
    mem_credentials.provision(str(DB_PATH), f"http://127.0.0.1:{port}")
    # Bind 127.0.0.1 by default (the host stance: localhost-only, operator owns
    # network controls). In the container set SC_BIND=0.0.0.0 so docker can
    # publish the port — the jail is the `-p 127.0.0.1:PORT:PORT` mapping, which
    # keeps it loopback-only on the host regardless of the in-container bind.
    bind = os.environ.get("SC_BIND", "127.0.0.1")
    require_loopback_bind(bind)
    import transport  # noqa: E402  (api/ — asyncio one-port multiplex)

    async def _serve():
        # Browser-native conversations are commit-woken, not interval-polled.
        # Startup/lease timers inside the broker exist only for bounded crash
        # reconciliation. Task #94 calls notify_commit() after its transaction.
        # The callback runs after the TCP bind succeeds, so a losing duplicate
        # server process cannot dispatch a queued prompt before bind refusal.
        def _start_runtime_services():
            conversation_broker.start_service(
                DB_PATH,
                launch_preparer=conversation_launch.ConversationLaunchPreparer(
                    DB_PATH
                ),
            )
            sprint_pr_watcher.start_service(DB_PATH, repo_root=REPO_ROOT)

        try:
            await transport.serve(
                bind,
                port,
                dispatch_http,
                _ws_unavailable,
                on_started=start_runtime_services,
                stream_handler=conversation_routes.stream_events,
            )
        finally:
            conversation_reaper.stop_service()

    print(
        f"super-coder review layer starting on 127.0.0.1:{port} "
        f"(bind {bind}, DB: {DB_PATH.name})"
    )
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
