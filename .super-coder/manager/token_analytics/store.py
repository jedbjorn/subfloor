"""Manager-owned cache and the cross-project dashboard queries."""
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import groupby

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project TEXT PRIMARY KEY, repo TEXT,
  pulled_at TEXT, error TEXT, warnings TEXT);
CREATE TABLE IF NOT EXISTS current_pins (project TEXT PRIMARY KEY, ref TEXT);
CREATE TABLE IF NOT EXISTS usage (
  project TEXT NOT NULL, usage_id INTEGER NOT NULL,
  harness TEXT, harness_session_ref TEXT, provider TEXT, model TEXT, title TEXT,
  started_at TEXT, ended_at TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  status TEXT, captured_at TEXT, archive_id INTEGER,
  shell TEXT, shell_session TEXT, flavor TEXT,
  PRIMARY KEY (project, usage_id));
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(COALESCE(started_at, ended_at, captured_at));
CREATE TABLE IF NOT EXISTS features (
  project TEXT NOT NULL, feature_id INTEGER NOT NULL, title TEXT, updated_at TEXT,
  has_doc INTEGER, PRIMARY KEY (project, feature_id));
CREATE TABLE IF NOT EXISTS specs (
  project TEXT NOT NULL, document_id INTEGER NOT NULL, title TEXT, frozen_date TEXT,
  feature_title TEXT, PRIMARY KEY (project, document_id));
CREATE TABLE IF NOT EXISTS engine_updates (
  project TEXT NOT NULL, event TEXT NOT NULL, ref TEXT NOT NULL, landed_at TEXT NOT NULL, subject TEXT,
  PRIMARY KEY (project, event));
-- chat turns + subagent runs as [start, end) intervals: the Shell Hours denominator
CREATE TABLE IF NOT EXISTS activity (
  project TEXT NOT NULL, source TEXT NOT NULL, shell TEXT,
  started_at TEXT NOT NULL, ended_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_activity_span ON activity(project, ended_at, started_at);
CREATE TABLE IF NOT EXISTS pulls (
  pull_id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, ok INTEGER, error TEXT);
"""

USAGE_COLS = ["usage_id", "harness", "harness_session_ref", "provider", "model", "title",
              "started_at", "ended_at", "input_tokens", "output_tokens", "cache_read_tokens",
              "cache_write_tokens", "reasoning_tokens", "status", "captured_at", "archive_id",
              "shell", "shell_session", "flavor"]

TS = "COALESCE(u.started_at, u.ended_at, u.captured_at)"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(database):
    con = sqlite3.connect(database, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def rows(cur):
    return [dict(r) for r in cur]


# ── pull ────────────────────────────────────────────────────────────────────

def ingest(con, p):
    stamp = now()
    name = p["project"]
    con.execute("INSERT INTO projects(project) VALUES (?) ON CONFLICT DO NOTHING", (name,))
    con.execute("UPDATE projects SET repo=? WHERE project=?",
                (p["repo"], name))
    if "current_ref" in p:
        con.execute("INSERT INTO current_pins(project,ref) VALUES (?,?) "
                    "ON CONFLICT(project) DO UPDATE SET ref=excluded.ref", (name, p["current_ref"]))
    for ref in p.get("engine_refs", []):
        # A commit can confirm a previously observed file event. Replace
        # only that dated marker, including markers from older cache versions.
        sha, landed = ref["ref"], ref["landed_at"]
        con.execute("DELETE FROM engine_updates WHERE project=? AND event IN (?,?) "
                    "AND ref=? AND landed_at=? AND event!=?",
                    (name, sha, f"pin:{sha}:{landed}", sha, landed, ref["event"]))
        con.execute("INSERT INTO engine_updates(project, event, ref, landed_at, subject) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(project,event) DO UPDATE SET "
                    "subject=COALESCE(excluded.subject, subject)",
                    (name, ref["event"], ref["ref"], ref["landed_at"], ref["subject"]))
    if p.get("fatal"):
        con.execute("UPDATE projects SET error=? WHERE project=?", (p["fatal"], name))
        return
    for table in ("usage", "features", "specs"):
        if table == "usage" or table in p:   # a failed side-read keeps its last rows
            con.execute(f"DELETE FROM {table} WHERE project=?", (name,))
    con.executemany(
        f"INSERT INTO usage(project, {', '.join(USAGE_COLS)}) VALUES (?{', ?' * len(USAGE_COLS)})",
        [[name] + [u.get(c) for c in USAGE_COLS] for u in p["usage"]])
    if "activity" in p:   # absent = the read failed: keep the last good rows
        con.execute("DELETE FROM activity WHERE project=?", (name,))
        con.executemany("INSERT INTO activity VALUES (?,?,?,?,?)",
                        [(name, a["source"], a.get("shell"), a["started_at"], a["ended_at"])
                         for a in p["activity"]])
    con.executemany("INSERT INTO features VALUES (?,?,?,?,?)",
                    [(name, f["feature_id"], f["title"], f["updated_at"], f["has_doc"])
                     for f in p.get("features", [])])
    con.executemany("INSERT INTO specs VALUES (?,?,?,?,?)",
                    [(name, s["document_id"], s["title"], s["frozen_date"], s["feature_title"])
                     for s in p.get("specs", [])])
    con.execute("UPDATE projects SET pulled_at=?, error=NULL, warnings=? WHERE project=?",
                (stamp, "; ".join(p.get("errors", [])) or None, name))


# ── reads (mirror the engine's /api/analytics/* shapes + a project filter) ────

def _arg(q, key):
    return (q.get(key, [""])[0] or "").strip()


def _where(q, alias="u"):
    conds, params = [], []
    for col in ("project", "harness", "model"):
        v = _arg(q, col)
        if v:
            conds.append(f"{alias}.{col}=?")
            params.append(v)
    return "".join(" AND " + c for c in conds), params


def _card_status(statuses):
    parts = set((statuses or "").split(","))
    if "partial" in parts:
        return "partial"
    if parts == {"no_usage"}:
        return "no_usage"
    return "ok"


def sessions(con, q):
    days = max(1, min(int(_arg(q, "days") or 7), 183))
    upper = _arg(q, "before") or now()
    lower = (datetime.fromisoformat(upper.replace("Z", "+00:00"))
             - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    where, params = _where(q)
    cards = rows(con.execute(
        f"SELECT u.project, u.harness, u.harness_session_ref, MIN({TS}) AS started_at, "
        "MAX(u.ended_at) AS ended_at, MAX(u.title) AS title, "
        "GROUP_CONCAT(DISTINCT u.model) AS models, GROUP_CONCAT(DISTINCT u.provider) AS providers, "
        "SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens, "
        "SUM(u.cache_read_tokens) AS cache_read_tokens, SUM(u.cache_write_tokens) AS cache_write_tokens, "
        "SUM(u.reasoning_tokens) AS reasoning_tokens, MAX(u.archive_id) AS archive_id, "
        "MAX(u.shell) AS shell, MAX(u.shell_session) AS shell_session, "
        "GROUP_CONCAT(DISTINCT u.status) AS statuses "
        f"FROM usage u WHERE {TS} >= ? AND {TS} < ?{where} "
        "GROUP BY u.project, u.harness, u.harness_session_ref ORDER BY started_at DESC",
        [lower, upper] + params))
    for c in cards:
        c["status"] = _card_status(c.pop("statuses"))
        c["unattributed"] = c["archive_id"] is None
    older = con.execute(f"SELECT 1 FROM usage u WHERE {TS} < ?{where} LIMIT 1",
                        [lower] + params).fetchone()
    return {"sessions": cards, "next_before": lower if older else None}


def filters(con, q):
    where, params = _where({"project": q.get("project", [""])})
    def distinct(col):
        return [r[0] for r in con.execute(
            f"SELECT DISTINCT u.{col} FROM usage u WHERE u.{col} IS NOT NULL{where} ORDER BY 1", params)]
    return {"projects": [r[0] for r in con.execute("SELECT project FROM projects ORDER BY project")],
            "harnesses": distinct("harness"), "models": distinct("model")}


def usage(con, q):
    frm_param, to_param = _arg(q, "from"), _arg(q, "to")
    window, wparams = "", []
    if frm_param:
        window += " AND substr({col}, 1, 10) >= ?"
        wparams.append(frm_param[:10])
    if to_param:
        window += " AND substr({col}, 1, 10) <= ?"
        wparams.append(to_param[:10])
    fav_window, fparams = "", []
    if frm_param:
        fav_window += f" AND {TS} >= ?"
        fparams.append(frm_param)
    if to_param:
        fav_window += f" AND {TS} < ?"
        fparams.append(to_param)
    where, params = _where(q)
    fav = {}
    for r in con.execute(
            "SELECT u.flavor, u.model, COUNT(DISTINCT u.project || '|' || u.harness || '|' || "
            "u.harness_session_ref) AS sessions FROM usage u "
            f"WHERE u.model IS NOT NULL AND u.flavor IS NOT NULL{fav_window}{where} "
            "GROUP BY u.flavor, u.model", fparams + params):
        if r["flavor"] not in fav or r["sessions"] > fav[r["flavor"]]["sessions"]:
            fav[r["flavor"]] = {"flavor": r["flavor"], "model": r["model"], "sessions": r["sessions"]}
    pwhere, pparams = _where({"project": q.get("project", [""])}, alias="t")
    features = rows(con.execute(
        "SELECT t.project, t.feature_id, t.title, t.updated_at FROM features t WHERE 1=1"
        + window.format(col="t.updated_at") + pwhere + " ORDER BY t.updated_at DESC",
        wparams + pparams))
    specs = rows(con.execute(
        "SELECT t.project, t.document_id, t.title, t.frozen_date, t.feature_title FROM specs t WHERE 1=1"
        + window.format(col="t.frozen_date") + pwhere + " ORDER BY t.frozen_date DESC",
        wparams + pparams))
    outstanding = rows(con.execute(
        "SELECT t.project, t.feature_id, t.title FROM features t WHERE t.has_doc=0"
        + pwhere + " ORDER BY t.updated_at DESC", pparams))
    return {"favorite_models": sorted(fav.values(), key=lambda f: f["flavor"]),
            "features_shipped": features, "specs_shipped": specs, "docs_outstanding": outstanding}


def activity(con, q):
    """Intervals overlapping [from, to); the page clips them to local days.
    Only the project filter applies: turns carry no harness or model."""
    where, params = _where({"project": q.get("project", [""])}, alias="a")
    for key, cond in (("from", "a.ended_at > ?"), ("to", "a.started_at < ?")):
        if _arg(q, key):
            where += f" AND {cond}"
            params.append(_arg(q, key))
    return {"activity": rows(con.execute(
        f"SELECT a.project, a.source, a.shell, a.started_at, a.ended_at FROM activity a WHERE 1=1{where}",
        params))}


def project_markers(observations):
    """Reconcile one checkout's file publications and Git confirmations in time order."""
    markers = []
    pending = {}
    last_is_file = False
    for row in observations:
        marker = dict(row)
        event = marker.pop("event")
        ref = marker["ref"]
        is_file = event.startswith("pin:") or event == ref  # legacy file key
        if is_file:
            if markers and markers[-1]["ref"] == ref:
                # A checkout can update the file mtime after its commit.
                if not last_is_file:
                    markers[-1]["landed_at"] = marker["landed_at"]
                    last_is_file = True
                continue
            pending[ref] = len(markers)
        elif ref in pending:
            # Keep the published marker. A later local transition may still
            # be waiting for its own commit, even when it returns to an old SHA.
            confirmed = pending[ref]
            if not markers[confirmed]["subject"]:
                markers[confirmed]["subject"] = marker["subject"]
            pending = {sha: i for sha, i in pending.items() if i > confirmed}
            continue
        else:
            pending.clear()
        markers.append(marker)
        last_is_file = is_file
    return markers


def engine_updates(con, q):
    where, params = _where({"project": q.get("project", [""])}, alias="e")
    observations = con.execute(
        f"SELECT rowid, e.* FROM engine_updates e WHERE 1=1{where} ORDER BY project, landed_at, rowid DESC", params)
    # Apply the chart window to publication times, after reconciliation.
    frm = _arg(q, "from")
    updates = [marker for _, project in groupby(observations, key=lambda row: row["project"])
               for marker in project_markers(project) if marker["landed_at"] >= frm]
    updates.sort(key=lambda u: (u["landed_at"], -u["rowid"]), reverse=True)
    for marker in updates:
        del marker["rowid"]
    return {"updates": updates}


def status(con):
    projects = rows(con.execute(
        "SELECT p.project, p.pulled_at, p.error, p.warnings, "
        "(SELECT COUNT(*) FROM usage u WHERE u.project=p.project) AS usage_rows, "
        "CASE WHEN c.project IS NOT NULL THEN c.ref ELSE "
        "(SELECT e.ref FROM engine_updates e WHERE e.project=p.project "
        " ORDER BY e.landed_at DESC LIMIT 1) END AS engine_ref "
        "FROM projects p LEFT JOIN current_pins c ON c.project=p.project ORDER BY p.project"))
    last = con.execute("SELECT * FROM pulls ORDER BY pull_id DESC LIMIT 1").fetchone()
    last_ok = con.execute("SELECT finished_at FROM pulls WHERE ok=1 ORDER BY pull_id DESC LIMIT 1").fetchone()
    return {"projects": projects, "last_pull": dict(last) if last else None,
            "last_ok_at": last_ok[0] if last_ok else None}
