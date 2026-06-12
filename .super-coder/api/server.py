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

sys.path.insert(0, str(ENGINE / "scripts"))
import ports as ports_mod  # noqa: E402
import shell_factory  # noqa: E402
import snapshot as snapshot_mod  # noqa: E402  (engine_skill_names — origin rule)

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
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


# Shell fields the review layer may write. seed/L&S/system_prompt/mandate are
# deliberately ABSENT — the law says the shell curates them, so there is no door.
SHELL_EDITABLE = {"current_state"}  # workspace + connections both retired (B5) → current_state is the one writable surface; "where things live" is the derived dr_* map
FLAG_EDITABLE = {"resolved", "resolution_notes", "description", "feature_id", "priority"}
ROADMAP_EDITABLE = {"title", "roadmap_status", "summary", "sort_order"}


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


# Funnel order: idea inlet → most-active committed work → done (shipped) →
# taken-off-the-board (retired). shipped = delivered; retired = chose not to.
_ORDER = ["brainstorm", "in_progress", "next", "near_term", "long_term", "shipped", "retired"]
_LABEL = {"brainstorm": "Brainstorm", "in_progress": "In Progress", "next": "Next",
          "near_term": "Near Term", "long_term": "Long Term", "shipped": "Shipped",
          "retired": "Retired"}


def get_roadmap(con) -> dict:
    feats = rows(con.execute(
        "SELECT r.feature_id, r.title, r.roadmap_status, r.sort_order, r.summary, "
        "s.shortname AS owner FROM roadmap r LEFT JOIN shells s "
        "ON s.shell_id=r.owning_shell ORDER BY r.sort_order, r.feature_id"))
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
    for f in feats:
        f["documents"] = docs_by.get(f["feature_id"], [])
        f["open_flags"] = flags_by.get(f["feature_id"], [])
    buckets = [{"status": s, "label": _LABEL[s],
                "features": [f for f in feats if f["roadmap_status"] == s]}
               for s in _ORDER]
    return {"buckets": [b for b in buckets if b["features"]]}


def get_docs(con) -> dict:
    """Documentation (kind='doc'), grouped client-side by feature. Distinct from
    the spec dev-cycle the roadmap tracks."""
    return {"docs": rows(con.execute(
        "SELECT d.document_id, d.feature_id, d.kind, d.seq, d.title, d.frozen, "
        "d.frozen_date, r.title AS feature_title FROM documents d "
        "LEFT JOIN roadmap r ON r.feature_id = d.feature_id "
        "WHERE d.kind='doc' ORDER BY d.feature_id, d.seq"))}


def get_map(con) -> dict:
    """The dr_* repo catalogue, summarized — how the shell (and the FnB) sees
    what's in the host repo."""
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
        p = subprocess.run(argv, capture_output=True, text=True,
                           cwd=str(REPO_ROOT), timeout=180)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "output": (p.stdout + p.stderr).strip() or "(no output)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": "timed out (>180s)"}


def run_snapshot_render() -> str:
    """The header 'snapshot ⤓' shortcut — serialize then render."""
    return (run_script("snapshot")["output"] + "\n"
            + run_script("render")["output"]).strip()


# ── Publish: serialize → commit → push → PR (the GUI "publish" button) ─────────
# Single-rolling-branch model: every GUI edit lands on PUBLISH_BRANCH with ONE
# open PR to main, so branches never proliferate and main stays clean until you
# merge. Push + PR need a GitHub token in the env (GH_TOKEN); `./sc launch`
# forwards it into the sandbox. Without a token the change is still COMMITTED
# locally — the tree never goes dirty — only the push/PR is skipped, with a clear
# message. A module lock serializes concurrent publishes (one git index).
BASE_BRANCH = "main"
PUBLISH_BRANCH = "gui-content"
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


def _git(*args):
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)


def _gh_token() -> str:
    return (os.environ.get("SC_GH_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


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

    # 1. serialize the DB → git-tracked text + render the flat files.
    out.append(run_snapshot_render())

    # 2. land on the rolling branch (created from HEAD on first publish; the
    #    operator stays on it — editing lives on gui-content, main stays clean).
    cur = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != PUBLISH_BRANCH:
        exists = _git("rev-parse", "--verify", "--quiet",
                      f"refs/heads/{PUBLISH_BRANCH}").returncode == 0
        sw = (_git("checkout", PUBLISH_BRANCH) if exists
              else _git("checkout", "-b", PUBLISH_BRANCH))
        if sw.returncode != 0:
            return {"ok": False, "output": "\n".join(out) +
                    f"\n\n✗ can't switch to '{PUBLISH_BRANCH}' from '{cur}':\n"
                    f"{sw.stderr.strip()}\n\nCheck out '{PUBLISH_BRANCH}' (or commit/"
                    "stash) before editing."}
        out.append(f"on branch {PUBLISH_BRANCH} (from {cur})")

    # 3. stage the publishable text + renders; commit if anything is new.
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
    if staged:
        n = len(staged.splitlines())
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = (f"gui: publish content edits ({n} file{'s' if n != 1 else ''})\n\n"
               f"Serialized + rendered from the review GUI at {stamp}.\n\n"
               + "\n".join(f"- {f}" for f in staged.splitlines()))
        c = _git("commit", "-m", msg)
        if c.returncode != 0:
            return {"ok": False, "output": "\n".join(out) +
                    "\n\n✗ commit failed:\n" + (c.stderr or c.stdout).strip()}
        out.append(f"committed {n} file(s)")
    else:
        out.append("no new content to commit")

    # 4. nothing ahead of main → nothing to publish.
    ahead = _git("rev-list", "--count",
                 f"{BASE_BRANCH}..{PUBLISH_BRANCH}").stdout.strip() or "0"
    if ahead == "0":
        return {"ok": True, "output": "\n".join(out) +
                f"\n\n✓ {PUBLISH_BRANCH} matches {BASE_BRANCH} — nothing to publish."}

    # 5. token gate: committed locally either way (tree clean), but push/PR needs it.
    token = _gh_token()
    if not token:
        return {"ok": True, "output": "\n".join(out) +
                f"\n\n⚠ committed on {PUBLISH_BRANCH} ({ahead} commit(s) ahead of "
                f"{BASE_BRANCH}), but no GH_TOKEN — can't push or open a PR. Set "
                "SC_GH_TOKEN, or `./sc launch` with a host gh login."}

    # 6. push over token-https (no ssh keys needed).
    url = _origin_https()
    if not url:
        return {"ok": False, "output": "\n".join(out) +
                "\n\n✗ no 'origin' remote to push to."}
    push_url = url.replace("https://", f"https://x-access-token:{token}@", 1)
    p = _git("push", push_url, f"{PUBLISH_BRANCH}:{PUBLISH_BRANCH}")
    if p.returncode != 0:
        return {"ok": False, "output": "\n".join(out) +
                "\n\n✗ push failed:\n" + _redact((p.stderr or p.stdout).strip(), token)}
    out.append(f"pushed {PUBLISH_BRANCH} → origin ({ahead} commit(s) ahead)")

    # 7. upsert ONE PR (gh reads the token from the env).
    env = {**os.environ, "GH_TOKEN": token}

    def gh(*args):
        return subprocess.run(["gh", *args], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, env=env)

    pr_url = gh("pr", "view", PUBLISH_BRANCH, "--json", "url", "-q", ".url").stdout.strip()
    if not pr_url:
        cr = gh("pr", "create", "--base", BASE_BRANCH, "--head", PUBLISH_BRANCH,
                "--title", "GUI content edits",
                "--body", "Rolling PR for content edited via the super-coder "
                "review GUI (roadmap, docs, flags, identity). Auto-updated on each "
                "publish; merge to land on main.")
        if cr.returncode != 0:
            return {"ok": False, "output": "\n".join(out) +
                    "\n\n✗ PR create failed:\n" + _redact((cr.stderr or cr.stdout).strip(), token)}
        pr_url = cr.stdout.strip()
        out.append(f"opened PR: {pr_url}")
    else:
        out.append(f"updated PR: {pr_url}")

    return {"ok": True, "output": "\n".join(out), "pr_url": pr_url}


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
                return self._send(200, get_map(con))
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
                return self._send(200, {"output": run_snapshot_render()})
            if path == "/api/publish":
                with _PUBLISH_LOCK:
                    r = git_publish()
                return self._send(200 if r["ok"] else 500, r)
            if path.startswith("/api/scripts/"):
                r = run_script(path.rsplit("/", 1)[1])
                if r is None:
                    return self._send(404, {"error": "no such script"})
                return self._send(200 if r["ok"] else 500, r)
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
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        con = db()
        try:
            if len(parts) == 5 and parts[1] == "shells" and parts[3] == "skills":
                set_grant(con, int(parts[2]), int(parts[4]),
                          bool(self._body().get("granted")))
                return self._send(200, {"ok": True})
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
