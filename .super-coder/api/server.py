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

import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"
UI_DIR = ENGINE / "ui"

sys.path.insert(0, str(ENGINE / "scripts"))
import ports as ports_mod  # noqa: E402

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Shell fields the review layer may write. seed/L&S/system_prompt/mandate are
# deliberately ABSENT — the law says the shell curates them, so there is no door.
SHELL_EDITABLE = {"current_state", "connections", "workspace"}
FLAG_EDITABLE = {"resolved", "resolution_notes", "description", "feature_id", "priority"}
ROADMAP_EDITABLE = {"title", "roadmap_status", "summary", "sort_order"}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ── Data assembly ─────────────────────────────────────────────────────────────

def get_shells(con) -> list[dict]:
    return rows(con.execute(
        "SELECT shell_id, display_name, shortname, role, mandate, is_shared "
        "FROM shells WHERE COALESCE(is_deleted,0)=0 ORDER BY shell_id"))


def get_shell(con, sid: int) -> dict | None:
    r = con.execute(
        "SELECT shell_id, display_name, shortname, partner, role, mandate, "
        "system_prompt, current_state, connections, workspace, lineage_seed, "
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
        "SELECT s.skill_id, s.name, s.description, "
        "(SELECT 1 FROM shell_skills ss WHERE ss.shell_id=? AND ss.skill_id=s.skill_id) "
        "AS granted FROM skills s WHERE s.is_deleted=0 ORDER BY s.name", (sid,)))
    shell["decisions"] = rows(con.execute(
        "SELECT decision_id, decision_date, priority, decision FROM shell_decisions "
        "WHERE shell_id=? AND COALESCE(is_deleted,0)=0 ORDER BY decision_id DESC "
        "LIMIT 25", (sid,)))
    return shell


_ORDER = ["next", "near_term", "long_term", "brainstorm", "shipped"]
_LABEL = {"next": "Next", "near_term": "Near term", "long_term": "Long term",
          "brainstorm": "Brainstorm", "shipped": "Shipped"}


def get_roadmap(con) -> dict:
    feats = rows(con.execute(
        "SELECT r.feature_id, r.title, r.roadmap_status, r.sort_order, r.summary, "
        "s.shortname AS owner FROM roadmap r LEFT JOIN shells s "
        "ON s.shell_id=r.owning_shell ORDER BY r.sort_order, r.feature_id"))
    docs_by: dict[int, list] = {}
    for d in rows(con.execute(
            "SELECT document_id, feature_id, kind, seq, title, frozen, frozen_date, "
            "render_path FROM documents ORDER BY feature_id, kind, seq")):
        docs_by.setdefault(d["feature_id"], []).append(d)
    flags_by: dict[int, list] = {}
    for f in rows(con.execute(
            "SELECT flag_id, display_name, description FROM flags "
            "WHERE resolved=0 AND COALESCE(is_deleted,0)=0 AND feature_id IS NOT NULL")):
        flags_by.setdefault(f["feature_id"], []).append(f)
    for f in feats:
        f["documents"] = docs_by.get(f["feature_id"], [])
        f["open_flags"] = flags_by.get(f["feature_id"], [])
    buckets = [{"status": s, "label": _LABEL[s],
                "features": [f for f in feats if f["roadmap_status"] == s]}
               for s in _ORDER]
    return {"buckets": [b for b in buckets if b["features"]]}


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


def run_snapshot_render():
    """Serialize + render after edits (manual precursor to the B6 automation)."""
    import subprocess
    out = []
    for script in ("snapshot.py", "render.py"):
        arg = ["flat"] if script == "render.py" else []
        p = subprocess.run([sys.executable, str(ENGINE / "scripts" / script), *arg],
                           capture_output=True, text=True)
        out.append((p.stdout + p.stderr).strip())
    return "\n".join(out)


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
            if path.startswith("/api/shells/"):
                sid = int(path.rsplit("/", 1)[1])
                shell = get_shell(con, sid)
                return self._send(200 if shell else 404,
                                  shell or {"error": "no such shell"})
            if path == "/api/roadmap":
                return self._send(200, get_roadmap(con))
            if path.startswith("/api/documents/"):
                did = int(path.rsplit("/", 1)[1])
                r = con.execute("SELECT * FROM documents WHERE document_id=?",
                                (did,)).fetchone()
                return self._send(200 if r else 404,
                                  dict(r) if r else {"error": "no such document"})
            if path == "/api/flags":
                return self._send(200, get_flags(con))
            return self._send(404, {"error": "not found"})
        except (ValueError, sqlite3.Error) as e:
            return self._send(400, {"error": str(e)})
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
            if path == "/api/snapshot":
                return self._send(200, {"output": run_snapshot_render()})
            return self._send(404, {"error": "not found"})
        except (ValueError, sqlite3.Error) as e:
            return self._send(400, {"error": str(e)})
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
        except (ValueError, sqlite3.Error) as e:
            return self._send(400, {"error": str(e)})
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
        except (ValueError, sqlite3.Error) as e:
            return self._send(400, {"error": str(e)})
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
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"super-coder review layer → http://127.0.0.1:{port}  (DB: {DB_PATH.name})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
