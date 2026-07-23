"""Interface HTTP API — /api/interface/* (spec #20, sprint 25 seq 5).

Sync route layer for the Interface vertical slice: sessions (New chat),
stream tickets, writer leases, clean certifications, termination,
reconciliation, browser-session bootstrap, and the generation hook callback.
Runs on the transport's executor threads (blocking sqlite, per-request
connections) and reaches the asyncio runtime only through its thread-safe
`call()` facade.

Authority (spec #20 API Resources / Security):
- reads + mutations: the operator bearer (`.super-coder/run/interface/
  operator.token`, mode 0600, provisioned at server boot) or a browser
  session cookie; mutations additionally need the session's anti-forgery
  token (X-CSRF).
- browser bootstrap (POST /browser-sessions) EXCHANGES the operator
  capability (bearer) for the cookie, and additionally requires same-origin
  fetch proof (Origin == Host, or Sec-Fetch-Site: same-origin) — a hostile
  site cannot mint a session, and a local process without the mode-0600
  token cannot either. The cookie is HttpOnly + SameSite=Strict.
- hook callbacks authenticate with the generation-scoped hook token only;
  it can call NOTHING else.
- every Interface route rejects a Host outside 127.0.0.1/localhost (DNS
  rebind) and a cross-site Origin/Sec-Fetch-Site on mutations.

Every mutation (hook-callbacks excepted — hook_seq is its idempotency)
requires Idempotency-Key; an exact retry replays the stored response, a key
reused with a different body returns 409 (interface_idempotency_keys).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
RUN_DIR = ENGINE / "run" / "interface"          # gitignored runtime home
OPERATOR_TOKEN_PATH = RUN_DIR / "operator.token"

sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "api"))
import db_driver  # noqa: E402
import interface_broker  # noqa: E402
import interface_hooks  # noqa: E402
import interface_state  # noqa: E402
import interface_wake  # noqa: E402
import ports as ports_mod  # noqa: E402
import shell_liveness  # noqa: E402

TICKET_TTL_S = 60
RESERVATION_TTL_S = 60
IDEM_TTL_S = 24 * 3600
_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")

_runtime = None          # bound by bind_runtime()
_browser_sessions: dict = {}
_browser_lock = threading.Lock()


# ------------------------------------------------------------------ plumbing

def bind_runtime(runtime) -> None:
    global _runtime
    _runtime = runtime
    runtime.on_unexpected_exit = _on_unexpected_exit


def _log(msg: str) -> None:
    print(f"[interface {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr,
          flush=True)


def ensure_operator_capability() -> str:
    """Provision the instance operator capability (mode 0600, owner-only).
    Server-side provisioning: server and CLI share this bind-mounted
    filesystem, so the file launch would write is the file the server reads.
    Idempotent — an existing token is kept (restart must not invalidate CLI
    scripts)."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    if not OPERATOR_TOKEN_PATH.exists():
        OPERATOR_TOKEN_PATH.write_text(secrets.token_hex(32))
        os.chmod(OPERATOR_TOKEN_PATH, 0o600)
    return OPERATOR_TOKEN_PATH.read_text().strip()


def _operator_token() -> str:
    try:
        return OPERATOR_TOKEN_PATH.read_text().strip()
    except OSError:
        return ""


def _db():
    return db_driver.connect(str(DB_PATH))


def _json(status: int, obj, headers=None):
    body = json.dumps(obj).encode()
    hdrs = [("Content-Type", "application/json")] + list(headers or [])
    return status, hdrs, body


def _err(status: int, code: str, message: str, details=None, headers=None):
    return _json(status, {"error": {"code": code, "message": message,
                                    "details": details or {}}}, headers)


def _parse_headers(headers_raw: str):
    import io
    return http.client.parse_headers(io.BytesIO(headers_raw.encode("latin-1")))


# ------------------------------------------------------------------ authority

class _Actor:
    def __init__(self, kind: str, scope: str, csrf_ok: bool,
                 shell_id: "int | None" = None):
        self.kind = kind          # "operator" | "browser" | "shell"
        self.scope = scope        # idempotency actor_scope
        self.csrf_ok = csrf_ok    # mutation authority for browser actors
        self.shell_id = shell_id  # set for kind="shell" (the planner's token)


def _host_ok(headers) -> bool:
    host = (headers.get("Host") or "").split(":")[0].strip("[]").lower()
    return host in {h.strip("[]") for h in _ALLOWED_HOSTS}


def _same_origin_proof(headers) -> bool:
    """The bootstrap's hostile-site fence: an explicit Origin must match the
    request's Host; without one, Sec-Fetch-Site (which browsers forbid sites
    from forging) must say same-origin or be absent (CLI)."""
    origin = headers.get("Origin")
    host = headers.get("Host") or ""
    if origin:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == host
    sfs = headers.get("Sec-Fetch-Site")
    return sfs in (None, "same-origin", "none")


def _mutation_site_ok(headers) -> bool:
    origin = headers.get("Origin")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != (headers.get("Host") or ""):
            return False
    sfs = headers.get("Sec-Fetch-Site")
    return sfs in (None, "same-origin", "none")


def _resolve_actor(headers) -> "_Actor | None":
    authz = headers.get("Authorization") or ""
    if authz[:7].lower() == "bearer ":
        token = authz[7:].strip()
        if token and token == _operator_token():
            return _Actor("operator", "operator", True)
        # A shell's own API token (the planner driving `sc sprint action` /
        # arming its own binding). Deliberately read-only elsewhere: shell
        # actors may call ONLY the sprint-binding and action-receipt routes
        # (enforced in handle()) — never session/writer/stop authority.
        if token:
            con = _db()
            try:
                row = con.execute(
                    "SELECT shell_id FROM shells WHERE api_key=? "
                    "AND COALESCE(is_deleted,0)=0", (token,)).fetchone()
            finally:
                con.close()
            if row is not None:
                return _Actor("shell", f"shell:{row[0]}", True,
                              shell_id=row[0])
        return None
    cookie = headers.get("Cookie") or ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "sc_if" and v:
            with _browser_lock:
                sess = _browser_sessions.get(v)
            if sess is None:
                return None
            csrf_ok = (headers.get("X-CSRF") or "") == sess["csrf"]
            return _Actor("browser", f"browser:{v[:16]}", csrf_ok)
    return None


# ------------------------------------------------------------------ idempotency

def _idempotent(con, actor: _Actor, operation: str, headers, body_obj,
                produce):
    """Idempotency-Key discipline for every Interface mutation: missing key →
    422; exact replay → the original response; key + different body → 409.
    `produce()` returns (status, obj) and runs ONLY on a fresh key."""
    key = headers.get("Idempotency-Key") or ""
    if not key:
        return _err(422, "idempotency_key_required",
                    "Idempotency-Key header is required for Interface mutations")
    canonical = hashlib.sha256(
        json.dumps(body_obj, sort_keys=True, default=str).encode()).hexdigest()
    row = con.execute(
        "SELECT request_hash, response_status, response_resource "
        "FROM interface_idempotency_keys "
        "WHERE actor_scope=? AND operation=? AND idem_key=?",
        (actor.scope, operation, key)).fetchone()
    if row is not None:
        if row[0] != canonical:
            return _err(409, "idempotency_conflict",
                        "Idempotency-Key reused with a different request body")
        return _json(row[1], json.loads(row[2]))
    status, obj = produce()
    try:
        con.execute(
            "INSERT INTO interface_idempotency_keys "
            "(actor_scope, operation, idem_key, request_hash, response_status, "
            " response_resource, expires_at) "
            "VALUES (?,?,?,?,?,?, datetime('now', ?))",
            (actor.scope, operation, key, canonical, status,
             json.dumps(obj, default=str), f"+{IDEM_TTL_S} seconds"))
        con.commit()
    except Exception:
        # A concurrent identical request won the insert race — replay ITS
        # stored response rather than double-acting (the PK is the backstop).
        con.rollback()
        row = con.execute(
            "SELECT response_status, response_resource "
            "FROM interface_idempotency_keys "
            "WHERE actor_scope=? AND operation=? AND idem_key=?",
            (actor.scope, operation, key)).fetchone()
        if row is not None:
            return _json(row[0], json.loads(row[1]))
        raise
    return _json(status, obj)


# ------------------------------------------------------------------ projection

def _alert_count(con, session_id) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM planner_alerts "
        "WHERE session_id=? AND resolved_at IS NULL", (session_id,)).fetchone()[0]


def _availability(con, shell_id: int, snap) -> dict:
    """The rail projection (spec #20 Occupancy Model): occupancy + lifecycle
    projected for compact display; New-chat authority never changes here. A
    shell with no live session is available ONLY after the liveness scan
    clears it — a legacy/unmanaged harness makes it unreconciled."""
    row = con.execute(
        "SELECT session_id, occupancy, lifecycle, harness "
        "FROM interface_sessions WHERE shell_id=? AND occupancy <> 'ended'",
        (shell_id,)).fetchone()
    if row is not None:
        session_id, occupancy, lifecycle, harness = row
        if occupancy == "reserved":
            availability = "starting"
        elif occupancy == "occupied":
            availability = "occupied"
        else:  # unreconciled
            availability = {"lost": "lost", "error": "error"}.get(
                lifecycle, "unreconciled")
        composer = con.execute(
            "SELECT composer FROM interface_input_state WHERE session_id=?",
            (session_id,)).fetchone()
        return {"availability": availability, "session_id": session_id,
                "lifecycle": lifecycle, "harness": harness,
                "composer": composer[0] if composer else None,
                "alerts": _alert_count(con, session_id)}
    state = shell_liveness.session_state(_shortname(con, shell_id), snap)
    if state is not None:
        return {"availability": "unreconciled", "session_id": None,
                "lifecycle": None, "harness": None, "composer": None,
                "alerts": 0}
    return {"availability": "available", "session_id": None,
            "lifecycle": None, "harness": None, "composer": None, "alerts": 0}


def _shortname(con, shell_id: int) -> str:
    row = con.execute("SELECT shortname FROM shells WHERE shell_id=?",
                      (shell_id,)).fetchone()
    return row[0] if row else ""


# ------------------------------------------------------------------ sessions

def _worktree_for(shortname: str, flavor: "str | None" = None) -> str:
    """A shell's exec cwd, resolved through the CLI boot's own rule
    (run.shell_work_dir): admin at the repo root, every other flavor at
    .sc-worktrees/<shortname>. Lazy import — run.py is the CLI module."""
    import run as run_mod
    return str(run_mod.shell_work_dir(shortname, flavor))


def _provision_worktree(worktree: str, shortname: str):
    """Validate, then create the shell's git worktree on demand, exactly
    like the CLI boot (run.ensure_worktree). A shell that was never
    CLI-booted has no .sc-worktrees/<shortname> yet; the tmux pane's `cd`
    and the exec both assume it exists, so it must be provisioned BEFORE
    the reservation is written — never left to fail as a raw 'not a
    directory'. Path validation distinguishes the three failure shapes:
    missing (provision it), existing-but-not-a-directory (refuse — a stray
    file blocks the path), and existing-but-unusable (a bare directory
    without git backing, or not writable — refuse, provisioning assumes an
    existing dir is intact and would no-op). Returns an error tuple for
    produce() on failure, None on success/no-op."""
    import run as run_mod
    path = Path(worktree)
    if path == run_mod.REPO_ROOT:
        return None  # admin flavor boots at the repo root — nothing to add

    def _err_tuple(code: str, message: str, reason: str):
        return 500, {"error": {
            "code": code, "message": message,
            "details": {"worktree": worktree, "shortname": shortname,
                        "reason": reason}}}

    if path.exists() and not path.is_dir():
        return _err_tuple(
            "worktree_not_directory",
            f"{worktree} exists but is not a directory — remove the stray "
            f"file, then retry New chat", "non_directory")
    if path.is_dir():
        if not (path / ".git").exists():
            return _err_tuple(
                "worktree_unusable",
                f"{worktree} is a plain directory, not a git worktree — "
                f"remove it or provision it by hand (`./sc enter "
                f"{shortname}`), then retry New chat", "not_a_worktree")
        if not os.access(path, os.W_OK | os.X_OK):
            return _err_tuple(
                "worktree_unusable",
                f"{worktree} is not writable by this service — fix its "
                f"ownership/permissions, then retry New chat",
                "not_writable")
        return None
    try:
        run_mod.ensure_worktree(path, shortname)
    except SystemExit as e:  # ensure_worktree exits with the git stderr
        detail = str(e).removeprefix("FATAL: ").strip()
    except (OSError, run_mod.LaunchError) as e:
        # The expected launcher failures (git binary missing, mkdir
        # refused, launch-gate refusal) — curated, never a raw 500.
        detail = f"{type(e).__name__}: {e}"
    else:
        return None
    return _err_tuple(
        "worktree_provision_failed",
        f"could not provision the shell worktree: {detail} — fix the repo "
        f"(`git worktree list`) or create it by hand (`./sc enter "
        f"{shortname}`), then retry New chat", "provision_failed")


def _create_session(actor, headers, body):
    if _runtime is None or not _runtime.available:
        reason = getattr(_runtime, "unavailable_reason", None) or "stack not loaded"
        return _err(503, "interface_unavailable",
                    f"Interface runtime unavailable: {reason}")
    shell_id = body.get("shell_id")
    if not isinstance(shell_id, int):
        return _err(422, "validation", "shell_id (int) is required")
    rows = int(body.get("rows") or 24)
    cols = int(body.get("cols") or 80)
    if not (1 <= rows <= 500 and 1 <= cols <= 500):
        return _err(422, "validation", "rows/cols out of range")
    unknown = set(body) - {"shell_id", "harness", "model", "effort",
                           "rows", "cols"}
    if unknown:
        return _err(422, "validation", f"unknown fields: {sorted(unknown)}")

    con = _db()
    try:
        shell = con.execute(
            "SELECT shell_id, shortname, flavor FROM shells "
            "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
            (shell_id,)).fetchone()
        if shell is None:
            return _err(404, "no_such_shell", f"shell {shell_id} not found")
        shortname = shell[1]
        flavor = shell[2]

        def produce():
            # These checks live INSIDE produce: the idempotency store is
            # consulted first, so an exact retry replays the original 201
            # instead of tripping the occupied check on its own session.
            existing = con.execute(
                "SELECT session_id, occupancy FROM interface_sessions "
                "WHERE shell_id=? AND occupancy <> 'ended'",
                (shell_id,)).fetchone()
            if existing is not None:
                return 409, {"error": {
                    "code": "shell_occupied",
                    "message": "a live or unreconciled generation already "
                               "owns this shell",
                    "details": {"session_id": existing[0],
                                "occupancy": existing[1]}}}
            # The legacy/unmanaged backstop (spec #20 Occupancy Model): a
            # harness process launched outside the API blocks New chat —
            # absence of a managed row is never proof of availability.
            snap = shell_liveness.compute()
            if shell_liveness.session_state(shortname, snap) is not None:
                return 409, {"error": {
                    "code": "unmanaged_harness",
                    "message": "a legacy or directly launched harness "
                               "process holds this shell's worktree — New "
                               "chat is blocked as unreconciled until "
                               "absence is proved",
                    "details": {"shortname": shortname}}}
            # Resolve + provision the shell's exec cwd through the CLI
            # boot's own rule BEFORE any row or token exists: a shell never
            # CLI-booted (e.g. a planner woken only through the Interface)
            # has no worktree yet — create it here, like `./sc enter`.
            worktree = _worktree_for(shortname, flavor)
            provision_err = _provision_worktree(worktree, shortname)
            if provision_err is not None:
                return provision_err
            gen_no = con.execute(
                "SELECT COALESCE(MAX(generation),0)+1 FROM "
                "interface_generations WHERE shell_id=?",
                (shell_id,)).fetchone()[0]
            hook_token = secrets.token_hex(24)
            harness = body.get("harness")
            con.execute(
                "INSERT INTO interface_generations "
                "(shell_id, generation, hook_token_hash) VALUES (?,?,?)",
                (shell_id, gen_no,
                 hashlib.sha256(hook_token.encode()).hexdigest()))
            cur = con.execute(
                "INSERT INTO interface_sessions "
                "(shell_id, generation, harness, model_route, worktree, "
                " occupancy, lifecycle, reservation_expires_at) "
                "VALUES (?,?,?,?,?, 'reserved', 'starting', "
                "        datetime('now', ?))",
                (shell_id, gen_no, harness, body.get("model"),
                 worktree, f"+{RESERVATION_TTL_S} seconds"))
            session_id = cur.lastrowid
            con.execute(
                "INSERT INTO interface_input_state "
                "(session_id, shell_id, generation) VALUES (?,?,?)",
                (session_id, shell_id, gen_no))
            con.commit()

            token_path = RUN_DIR / f"launch-{session_id}.json"
            token_path.write_text(json.dumps({
                "session_id": session_id, "shell_id": shell_id,
                "generation": gen_no, "hook_token": hook_token,
                "api_port": ports_mod.resolve().get("port", 8800),
                "worktree": worktree,
                "harness": harness, "model": body.get("model"),
                "effort": body.get("effort")}))
            os.chmod(token_path, 0o600)
            try:
                identity = _runtime.call(_runtime.spawn(
                    session_id=session_id, shell_id=shell_id,
                    generation=gen_no, worktree=worktree,
                    sc_path=str(REPO_ROOT / "sc"),
                    token_path=str(token_path), rows=rows, cols=cols))
            except Exception as exc:  # noqa: BLE001 — see below
                # Definite pre-spawn failure (runtime down, bad worktree)
                # closes the reservation through the one closure helper —
                # occupancy AND lifecycle terminalize together; an
                # AMBIGUOUS tmux outcome leaves it unreconciled (spec #20
                # Interface Workflow 4) — never a second process, never an
                # auto-kill.
                from interface_runtime import InterfaceUnavailable
                definite = isinstance(exc, (InterfaceUnavailable,
                                            FileNotFoundError, ValueError))
                if definite:
                    interface_broker.close_session(con, session_id,
                                                   "spawn_failed")
                    con.execute(
                        "UPDATE interface_sessions SET error_detail=? "
                        "WHERE session_id=?",
                        (f"spawn failed: {exc}"[:400], session_id))
                else:
                    interface_state.transition(
                        con, "occupancy", session_id, "unreconciled",
                        extra_sets={"error_detail":
                                    f"spawn failed: {exc}"[:400]})
                con.commit()
                code = 503 if definite else 202
                return code, {"session_id": session_id, "shell_id": shell_id,
                              "occupancy": "ended" if definite else "unreconciled",
                              "error": str(exc)[:200]}
            con.execute(
                "UPDATE interface_sessions SET tmux_socket=?, tmux_session=?, "
                "tmux_window=?, tmux_pane_id=?, pane_pid=?, pane_start_ticks=? "
                "WHERE session_id=?",
                (identity["tmux_socket"], identity["tmux_session"],
                 identity["tmux_window"], identity["pane_id"],
                 identity["pane_pid"], identity["pane_start_ticks"],
                 session_id))
            con.commit()
            return 201, {"session_id": session_id, "shell_id": shell_id,
                         "generation": gen_no, "occupancy": "reserved",
                         "lifecycle": "starting", "harness": harness}

        result = _idempotent(con, actor, "create_session", headers, body,
                             produce)
        if result[0] == 201:
            result[1].append(("Location",
                              f"/api/interface/sessions/"
                              f"{json.loads(result[2])['session_id']}"))
        return result
    except Exception as exc:  # noqa: BLE001
        import sqlite3
        if isinstance(exc, sqlite3.IntegrityError):
            # Lost the reservation race — the partial unique index fired;
            # return the existing owner (spec: a concurrent start returns the
            # existing owner, never a second process). owner can momentarily
            # be None if the winner committed only its generation row so far
            # (the generations index fired first) — the retry reveals it.
            owner = con.execute(
                "SELECT session_id, occupancy FROM interface_sessions "
                "WHERE shell_id=? AND occupancy <> 'ended'",
                (shell_id,)).fetchone()
            return _err(409, "shell_occupied",
                        "a concurrent start owns this shell",
                        {"session_id": owner[0] if owner else None,
                         "occupancy": owner[1] if owner else None})
        raise
    finally:
        con.close()


def _now(con) -> str:
    return con.execute("SELECT datetime('now')").fetchone()[0]


def _get_session(session_id: int):
    con = _db()
    try:
        row = con.execute(
            "SELECT session_id, shell_id, generation, archive_id, harness, "
            "model_route, worktree, occupancy, lifecycle, created_at, "
            "occupied_at, ended_at, end_reason, error_detail "
            "FROM interface_sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if row is None:
            return _err(404, "no_such_session",
                        f"interface session {session_id} not found")
        istate = con.execute(
            "SELECT composer, delivery, forwarded_seq, last_human_input_at "
            "FROM interface_input_state WHERE session_id=?",
            (session_id,)).fetchone()
        writer = interface_broker.current_writer(con, session_id)
        runtime_state = (_runtime.runtime_state(session_id)
                         if _runtime is not None else None)
        return _json(200, {
            "session_id": row[0], "shell_id": row[1], "generation": row[2],
            "archive_id": row[3], "harness": row[4], "model_route": row[5],
            "worktree": row[6], "occupancy": row[7], "lifecycle": row[8],
            "created_at": row[9], "occupied_at": row[10],
            "ended_at": row[11], "end_reason": row[12],
            "error_detail": row[13],
            "composer": istate[0] if istate else None,
            "delivery": istate[1] if istate else None,
            "forwarded_seq": istate[2] if istate else None,
            "last_human_input_at": istate[3] if istate else None,
            "writer": {"held": writer is not None,
                       "client_id": writer[1] if writer else None},
            "wake_state": _wake_state(con, session_id=session_id),
            "clients": (runtime_state or {}).get("attached_clients", 0),
            "alerts": _alert_count(con, session_id),
        })
    finally:
        con.close()


def _list_shells():
    con = _db()
    try:
        snap = shell_liveness.compute()
        shells = con.execute(
            "SELECT shell_id, shortname, display_name FROM shells "
            "WHERE COALESCE(is_deleted,0)=0 ORDER BY shell_id").fetchall()
        out = []
        for shell_id, shortname, display_name in shells:
            proj = _availability(con, shell_id, snap)
            out.append({"shell_id": shell_id, "shortname": shortname,
                        "display_name": display_name,
                        "wake_state": _wake_state(
                            con, planner_shell_id=shell_id), **proj})
        return _json(200, {"shells": out})
    finally:
        con.close()


# ------------------------------------------------------------------ leases + tickets

def _acquire_lease(actor, headers, body):
    session_id = body.get("session_id")
    client_id = body.get("client_id")
    takeover = bool(body.get("takeover"))
    if not isinstance(session_id, int) or not client_id:
        return _err(422, "validation",
                    "session_id (int) and client_id are required")
    con = _db()
    try:
        def produce():
            token = secrets.token_hex(24)
            try:
                lease_id = interface_broker.acquire_writer(
                    con, session_id, str(client_id), token, takeover=takeover)
                con.commit()
            except interface_broker.BrokerError as exc:
                return 409, {"error": {"code": "lease_refused",
                                       "message": str(exc), "details": {}}}
            seq = con.execute(
                "SELECT next_input_seq FROM interface_writer_leases "
                "WHERE lease_id=?", (lease_id,)).fetchone()[0]
            return 201, {"lease_id": lease_id, "lease_token": token,
                         "next_input_seq": seq}
        return _idempotent(con, actor, "acquire_lease", headers, body, produce)
    finally:
        con.close()


def _release_lease(actor, headers, body, lease_id: int):
    token = body.get("lease_token") or ""
    con = _db()
    try:
        row = con.execute(
            "SELECT token_hash, revoked_at FROM interface_writer_leases "
            "WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None or row[1] is not None:
            return _err(404, "no_such_lease", f"lease {lease_id} not found")
        if hashlib.sha256(token.encode()).hexdigest() != row[0]:
            return _err(403, "lease_token_mismatch",
                        "a caller releases only its own lease")
        def produce():
            con.execute(
                "UPDATE interface_writer_leases SET revoked_at=datetime('now'),"
                " revoke_reason='released' WHERE lease_id=?", (lease_id,))
            con.commit()
            return 204, {}
        return _idempotent(con, actor, "release_lease", headers, body, produce)
    finally:
        con.close()


def _mint_ticket(actor, headers, body):
    if _runtime is None or not _runtime.available:
        return _err(503, "interface_unavailable", "Interface runtime unavailable")
    session_id = body.get("session_id")
    role = body.get("role")
    client_id = body.get("client_id")
    if not isinstance(session_id, int) or role not in ("viewer", "writer") \
            or not client_id:
        return _err(422, "validation",
                    "session_id, role (viewer|writer), client_id required")
    con = _db()
    try:
        sess = con.execute(
            "SELECT occupancy FROM interface_sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if sess is None:
            return _err(404, "no_such_session",
                        f"interface session {session_id} not found")
        lease_id = None
        lease_token = None
        if role == "writer":
            lease = interface_broker.current_writer(con, session_id)
            token = body.get("lease_token") or ""
            row = con.execute(
                "SELECT token_hash FROM interface_writer_leases "
                "WHERE lease_id=?", (lease[0],)).fetchone() if lease else None
            if lease is None or row is None or \
                    hashlib.sha256(token.encode()).hexdigest() != row[0]:
                return _err(403, "writer_requires_lease",
                            "a writer ticket needs the current lease_token")
            lease_id, lease_token = lease[0], token

        def produce():
            ticket = _runtime.mint_ticket(
                session_id=session_id, role=role, client_id=str(client_id),
                lease_id=lease_id, lease_token=lease_token)
            return 201, ticket
        return _idempotent(con, actor, "mint_ticket", headers, body, produce)
    finally:
        con.close()


# ------------------------------------------------------------------ certify / terminate / reconcile

def _certify_clean(actor, headers, body):
    session_id = body.get("session_id")
    client_id = body.get("client_id")
    client_seq = body.get("client_seq")
    if not isinstance(session_id, int) or not client_id \
            or not isinstance(client_seq, int):
        return _err(422, "validation",
                    "session_id, client_id, client_seq (int) required")
    con = _db()
    try:
        lease = interface_broker.current_writer(con, session_id)
        if lease is None or lease[1] != str(client_id):
            return _err(409, "not_the_writer",
                        "clean certification rides the current writer lease")
        def produce():
            try:
                interface_broker.certify_clean(
                    con, session_id, str(client_id), client_seq)
                con.commit()
            except interface_broker.BrokerError as exc:
                return 409, {"error": {"code": "certify_refused",
                                       "message": str(exc), "details": {}}}
            # A fresh clean certification may unblock queued wake work.
            interface_wake.notify_session(session_id)
            return 201, {"session_id": session_id, "composer": "clean"}
        return _idempotent(con, actor, "certify_clean", headers, body, produce)
    finally:
        con.close()


def _terminate(actor, headers, body):
    session_id = body.get("session_id")
    force = bool(body.get("force"))
    if not isinstance(session_id, int):
        return _err(422, "validation", "session_id (int) required")
    if _runtime is None or not _runtime.available:
        return _err(503, "interface_unavailable", "Interface runtime unavailable")
    con = _db()
    try:
        sess = con.execute(
            "SELECT shell_id, generation, occupancy, lifecycle, "
            "graceful_timed_out_at FROM interface_sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if sess is None:
            return _err(404, "no_such_session",
                        f"interface session {session_id} not found")
        if sess[2] == "unreconciled":
            return _err(409, "not_occupied",
                        f"session {session_id} is unreconciled — termination "
                        "needs a verified identity; reconcile and close "
                        "after proved absence instead")
        if force and sess[4] is None and sess[2] != "ended" \
                and sess[3] != "ended":
            # Spec Workflow 9: force only AFTER graceful termination fails
            # and shows the PID/generation it will end. The UI sequences it;
            # the API (authority surface for the seq-6 CLI) enforces it.
            return _err(409, "force_requires_graceful_timeout",
                        "force is available only after a graceful "
                        "termination timed out for this session")

        def produce():
            # Re-read inside produce: the gate above ran before the
            # idempotency store, and a hook/close may have raced in since.
            cur = con.execute(
                "SELECT occupancy, lifecycle, tmux_pane_id, pane_pid "
                "FROM interface_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            occ, lif, pane_id, pane_pid = cur
            if occ == "ended" or lif == "ended":
                # Terminal already (a repeated request, or a session_end
                # hook that won the race): complete durable closure
                # idempotently — never transition back to stopping (#532).
                interface_broker.close_session(con, session_id, "operator_end")
                con.commit()
                return 202, {"terminated": True, "already_ended": True}
            if occ == "reserved":
                # Cancel start (spec Lifecycle Contract / #519).
                if pane_id is None and pane_pid is None:
                    # No pane or harness identity was ever established:
                    # nothing live to signal — cancel the reservation.
                    interface_broker.close_session(
                        con, session_id, "cancelled_before_spawn")
                    con.commit()
                    _runtime.call(_runtime.abandon(session_id))
                    return 202, {"terminated": True,
                                 "end_reason": "cancelled_before_spawn"}
                if not _runtime.call(_runtime.verify_identity(session_id)):
                    # Spawn outcome uncertain — never silently ended: park
                    # as unreconciled and require absence proof.
                    interface_state.transition(
                        con, "occupancy", session_id, "unreconciled",
                        extra_sets={"error_detail":
                                    "cancel start: pane identity "
                                    "unverifiable"})
                    interface_state.transition(con, "lifecycle", session_id,
                                               "lost")
                    con.commit()
                    return 409, {"error": {
                        "code": "identity_unverified",
                        "message": "the reserved pane's identity cannot be "
                                   "verified — the session is unreconciled; "
                                   "prove absence and close it via "
                                   "reconciliation",
                        "details": {"session_id": session_id}}}
                # Verified identity live — the normal stop path below runs.
            try:
                interface_state.transition(con, "lifecycle", session_id,
                                           "stopping")
                con.commit()
                result = _runtime.call(_runtime.terminate(session_id,
                                                          force=force))
                if not result.get("terminated"):
                    reason = result.get("reason")
                    if reason == "identity_mismatch":
                        # Fail closed (spec: never kill an uncertain
                        # process) — the session is uncertain, not endable.
                        interface_state.transition(con, "occupancy",
                                                   session_id, "unreconciled")
                        interface_state.transition(con, "lifecycle",
                                                   session_id, "lost")
                        con.commit()
                        return 409, {"terminated": False,
                                     "reason": "identity_mismatch"}
                    if reason == "not_running":
                        # The runtime holds no live generation — absence,
                        # not a timeout. Prove it and converge rather than
                        # recording a phantom graceful timeout.
                        if _runtime.call(_runtime.prove_absence(session_id)):
                            interface_broker.close_session(
                                con, session_id, "operator_end")
                            con.commit()
                            _runtime.call(_runtime.abandon(session_id))
                            return 202, {"terminated": True,
                                         "reason": "already_absent"}
                        interface_state.transition(con, "occupancy",
                                                   session_id, "unreconciled")
                        interface_state.transition(con, "lifecycle",
                                                   session_id, "lost")
                        con.commit()
                        return 409, {"terminated": False,
                                     "reason": "not_running"}
                    # graceful timeout: stays stopping, and the timeout is
                    # recorded durably — it is what unlocks the force
                    # follow-up.
                    interface_state.transition(
                        con, "lifecycle", session_id, "stopping",
                        extra_sets={"graceful_timed_out_at": _now(con)})
                    con.commit()
                    return 200, {"terminated": False,
                                 "reason": result.get("reason",
                                                      "graceful_timeout"),
                                 "pid": result.get("pid"),
                                 "generation": result.get("generation")}
                interface_broker.close_session(
                    con, session_id,
                    "operator_force" if force else "operator_end")
                con.commit()
                return 202, {"terminated": True}
            except interface_state.InterfaceTransitionError:
                # A session_end hook (or a concurrent close) terminalized
                # mid-flight — the race the old code lost as a false
                # no_such_route (#532). Roll back the partial attempt and
                # converge closure; the semantic result is still success.
                con.rollback()
                interface_broker.close_session(con, session_id, "operator_end")
                con.commit()
                _runtime.call(_runtime.abandon(session_id))
                return 202, {"terminated": True, "already_ended": True}

        return _idempotent(con, actor, "terminate", headers, body, produce)
    finally:
        con.close()


def _reconcile(actor, headers, body):
    session_id = body.get("session_id")
    action = body.get("action", "verify")
    if not isinstance(session_id, int) or action not in ("verify", "close"):
        return _err(422, "validation",
                    "session_id (int) required; action is verify|close")
    if _runtime is None or not _runtime.available:
        return _err(503, "interface_unavailable", "Interface runtime unavailable")
    con = _db()
    try:
        sess = con.execute(
            "SELECT shell_id, generation, occupancy, lifecycle, tmux_pane_id,"
            " pane_pid, pane_start_ticks FROM interface_sessions "
            "WHERE session_id=?", (session_id,)).fetchone()
        if sess is None:
            return _err(404, "no_such_session",
                        f"interface session {session_id} not found")
        if sess[2] == "ended":
            return _err(409, "session_ended",
                        f"session {session_id} has ended")

        if action == "close":
            # The road OUT of unreconciled (spec Occupancy Model: "the
            # operator closes or replaces it"; Interface Layout: lost/error
            # panes offer close/fresh-generation). Close is allowed only
            # after absence is PROVED — anything uncertain refuses.
            if sess[2] != "unreconciled":
                return _err(409, "not_unreconciled",
                            f"session {session_id} is {sess[2]} — close "
                            "applies to unreconciled sessions only "
                            "(occupied sessions end via termination)")

            def produce_close():
                absent = _runtime.call(_runtime.prove_absence(session_id))
                if not absent:
                    con.commit()
                    return 409, {"error": {
                        "code": "absence_not_proved",
                        "message": "the pane or its exact-identity process "
                                   "is still present — close refused, "
                                   "reconcile or investigate first",
                        "details": {}}}
                _runtime.call(_runtime.abandon(session_id))
                interface_broker.close_session(con, session_id,
                                               "operator_close")
                con.commit()
                return 200, {"session_id": session_id, "closed": True,
                             "occupancy": "ended",
                             "actions": ["absence proved — session closed; "
                                         "the shell offers New chat again"]}
            return _idempotent(con, actor, "close_session", headers, body,
                               produce_close)

        def produce():
            verified = _runtime.call(_runtime.verify_identity(session_id))
            if verified:
                actions = []
                if sess[2] == "unreconciled":
                    interface_state.transition(con, "occupancy", session_id,
                                               "occupied")
                    actions.append("occupancy: unreconciled → occupied")
                con.commit()
                return 200, {"session_id": session_id, "verified": True,
                             "occupancy": "occupied", "actions": actions}
            con.commit()
            return 200, {"session_id": session_id, "verified": False,
                         "occupancy": sess[2],
                         "actions": ["identity could not be verified — "
                                     "fail-closed, no state changed"]}
        return _idempotent(con, actor, "reconcile", headers, body, produce)
    finally:
        con.close()


# ------------------------------------------------------------------ hooks

# ------------------------------------------------------------------ sprint wake

def _wake_state(con, *, session_id=None, planner_shell_id=None) -> str:
    """The occupancy model's wake dimension, derived (never stored):
    disarmed without an unreleased binding; parked while a batch is
    delivery_unknown; the live batch's state while one submits/runs;
    queued with pending work; else armed."""
    if session_id is not None:
        binding = con.execute(
            "SELECT binding_id FROM sprint_planner_bindings "
            "WHERE session_id=? AND released_at IS NULL",
            (session_id,)).fetchone()
    else:
        binding = con.execute(
            "SELECT binding_id FROM sprint_planner_bindings "
            "WHERE planner_shell_id=? AND released_at IS NULL",
            (planner_shell_id,)).fetchone()
    if binding is None:
        return "disarmed"
    # A parked batch shadows every newer live one — a parked batch is not
    # 'live' per idx_pwb_live, so the drain forms a NEW batch while one
    # parks, and a newest-first pick would hide the park behind it.
    if con.execute(
            "SELECT 1 FROM planner_wake_batches WHERE binding_id=? "
            "AND state='delivery_unknown' LIMIT 1", (binding[0],)).fetchone():
        return "parked"
    batch = con.execute(
        "SELECT state FROM planner_wake_batches WHERE binding_id=? "
        "AND state IN ('queued','submitting','running') "
        "ORDER BY batch_id DESC LIMIT 1", (binding[0],)).fetchone()
    if batch is not None:
        if batch[0] in ("submitting", "running"):
            return batch[0]
        return "queued"
    queued = con.execute(
        "SELECT 1 FROM planner_wake_items WHERE binding_id=? "
        "AND state='queued' LIMIT 1", (binding[0],)).fetchone()
    return "queued" if queued is not None else "armed"


def _err_obj(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "details": {}}}


def _arm_binding(actor, headers, body):
    """POST /api/interface/sprint-bindings — arm one ACTIVE sprint document
    to one planner generation (spec #20 API Resources / Sprint Scope). A
    shell actor may arm only ITSELF; the operator may arm any planner.
    Fail-closed refusals: doc missing/frozen/not ACTIVE, no occupied
    planner session, a mandatory-hook gap (an unverifiable wake must never
    arm), or a second ACTIVE binding (the partial unique indexes backstop).
    """
    doc_id = body.get("sprint_doc_id")
    planner = body.get("planner_shell_id")
    if not isinstance(doc_id, int) or not isinstance(planner, int):
        return _err(422, "validation",
                    "sprint_doc_id, planner_shell_id (int) required")
    if actor.kind == "shell" and actor.shell_id != planner:
        return _err(403, "not_the_planner",
                    "a shell may arm only its own binding")
    con = _db()
    try:
        def produce():
            doc = con.execute(
                "SELECT frozen FROM documents WHERE document_id=?",
                (doc_id,)).fetchone()
            if doc is None or doc[0] or not interface_broker._sprint_active(
                    con, doc_id):
                return 409, _err_obj(
                    "sprint_not_active",
                    "sprint document is missing, frozen, or not ACTIVE")
            sess = con.execute(
                "SELECT session_id, shell_id, generation, harness, "
                "cli_version FROM interface_sessions "
                "WHERE shell_id=? AND occupancy='occupied'",
                (planner,)).fetchone()
            if sess is None:
                return 409, _err_obj(
                    "no_live_session",
                    "planner has no occupied Interface session to bind")
            cap = interface_hooks.capability(sess[3], sess[4])
            if not cap["mandatory_ok"]:
                return 409, _err_obj(
                    "hooks_unsupported",
                    f"harness {sess[3]!r} lacks mandatory lifecycle hooks "
                    f"({', '.join(cap['missing_mandatory']) or 'version'}) — "
                    "sprint wake cannot arm")
            try:
                cur = con.execute(
                    "INSERT INTO sprint_planner_bindings "
                    "(sprint_doc_id, planner_shell_id, session_id, shell_id,"
                    " generation) VALUES (?,?,?,?,?)",
                    (doc_id, planner, sess[0], sess[1], sess[2]))
            except db_driver.IntegrityError:
                return 409, _err_obj(
                    "already_armed",
                    "planner or sprint already has an ACTIVE binding")
            con.commit()
            _log(f"sprint binding {cur.lastrowid} armed: doc={doc_id} "
                 f"planner={planner} session={sess[0]} gen={sess[2]}")
            return 201, {"binding_id": cur.lastrowid,
                         "sprint_doc_id": doc_id,
                         "planner_shell_id": planner,
                         "session_id": sess[0], "generation": sess[2],
                         "wake_state": "armed"}
        resp = _idempotent(con, actor, "sprint_binding_arm", headers, body,
                           produce)
        if resp[0] == 201:
            interface_wake.notify_binding(
                json.loads(resp[2])["binding_id"])
        return resp
    finally:
        con.close()


def _release_binding(actor, headers, body, binding_id: int):
    """DELETE /api/interface/sprint-bindings/{id} — release the binding and
    cancel its queued wake work with an audit reason (spec Sprint Scope:
    messages stay UNREAD; a live submitting/running batch is left for hook
    reconciliation — its fenced evidence still resolves it)."""
    con = _db()
    try:
        def produce():
            row = con.execute(
                "SELECT planner_shell_id, released_at "
                "FROM sprint_planner_bindings WHERE binding_id=?",
                (binding_id,)).fetchone()
            if row is None:
                return 404, _err_obj("no_such_binding",
                                     f"binding {binding_id} not found")
            if actor.kind == "shell" and actor.shell_id != row[0]:
                return 403, _err_obj(
                    "not_the_planner",
                    "a shell may release only its own binding")
            if row[1] is not None:
                return 200, {"binding_id": binding_id, "released": True,
                             "already_released": True}
            reason = (body.get("reason") or "operator release").strip()
            cancelled = interface_broker.release_binding(
                con, binding_id, reason)
            con.commit()
            _log(f"sprint binding {binding_id} released ({reason}); "
                 f"{cancelled} queued wake item(s) cancelled")
            return 200, {"binding_id": binding_id, "released": True,
                         "cancelled_items": cancelled,
                         "wake_state": "disarmed"}
        return _idempotent(con, actor, "sprint_binding_release", headers,
                           body, produce)
    finally:
        con.close()


# ---------------------------------------------------------- wake ops (seq 10)


def _qint(query: dict, name: str) -> "int | None":
    raw = query.get(name, [None])[0]
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _project_binding(con, row) -> dict:
    """The wake-status projection for one binding (spec #20 Wake Delivery /
    Data Model, sprint 25 seq 10): binding + sprint doc + derived wake_state
    + the current batch and last outcome + park/quarantine detail. Pure
    READ — no writer, no side effect."""
    (binding_id, doc_id, planner, session_id, generation, armed_at,
     released_at, release_reason) = row
    doc = con.execute(
        "SELECT title, frozen FROM documents WHERE document_id=?",
        (doc_id,)).fetchone()
    items = dict(con.execute(
        "SELECT state, COUNT(*) FROM planner_wake_items WHERE binding_id=? "
        "GROUP BY state", (binding_id,)).fetchall())
    current = con.execute(
        "SELECT batch_id, state, created_at, submitted_at "
        "FROM planner_wake_batches WHERE binding_id=? "
        "AND state IN ('queued','submitting','running') "
        "ORDER BY batch_id DESC LIMIT 1", (binding_id,)).fetchone()
    last = con.execute(
        "SELECT batch_id, state, completed_at FROM planner_wake_batches "
        "WHERE binding_id=? AND state IN ('complete','delivery_unknown') "
        "ORDER BY batch_id DESC LIMIT 1", (binding_id,)).fetchone()
    out = {
        "binding_id": binding_id,
        "sprint_doc_id": doc_id,
        "planner_shell_id": planner,
        "session_id": session_id,
        "generation": generation,
        "armed_at": armed_at,
        "released_at": released_at,
        "release_reason": release_reason,
        "sprint": {
            "document_id": doc_id,
            "title": doc[0] if doc else None,
            "frozen": bool(doc[1]) if doc else None,
            "active": interface_broker._sprint_active(con, doc_id),
        },
        "wake_state": ("disarmed" if released_at is not None else
                       _wake_state(con, planner_shell_id=planner)),
        "items": items,
        "current_batch": None,
        "last_batch": None,
        "park": None,
        "quarantined": [],
        "retry": {"applicable": False, "needs_outcome": False},
    }
    if current is not None:
        out["current_batch"] = {
            "batch_id": current[0], "state": current[1],
            "created_at": current[2], "submitted_at": current[3]}
    if last is not None:
        outcomes = dict(con.execute(
            "SELECT state, COUNT(*) FROM planner_wake_items "
            "WHERE batch_id=? GROUP BY state", (last[0],)).fetchall())
        out["last_batch"] = {"batch_id": last[0], "state": last[1],
                             "completed_at": last[2], "items": outcomes}
    quarantined = con.execute(
        "SELECT item_id, message_id, error, completed_wakes "
        "FROM planner_wake_items WHERE binding_id=? AND state='quarantined' "
        "ORDER BY item_id", (binding_id,)).fetchall()
    out["quarantined"] = [
        {"item_id": q[0], "message_id": q[1], "error": q[2],
         "completed_wakes": q[3]} for q in quarantined]
    if released_at is not None:
        return out
    input_park = con.execute(
        "SELECT delivery FROM interface_input_state WHERE session_id=?",
        (session_id,)).fetchone()
    input_park = input_park is not None and input_park[0] == "delivery_unknown"
    # The park surfaces regardless of any NEWER live batch — a parked batch
    # is not 'live' per idx_pwb_live, so one can coexist with the batch the
    # drain formed after the park.
    parked_row = con.execute(
        "SELECT batch_id FROM planner_wake_batches WHERE binding_id=? "
        "AND state='delivery_unknown' ORDER BY batch_id DESC LIMIT 1",
        (binding_id,)).fetchone()
    parked = parked_row is not None
    if parked or input_park:
        reason = con.execute(
            "SELECT reason FROM planner_alerts "
            "WHERE resolved_at IS NULL AND (binding_id=? OR session_id=?) "
            "ORDER BY alert_id DESC LIMIT 1",
            (binding_id, session_id)).fetchone()
        out["park"] = {
            "batch_id": parked_row[0] if parked else None,
            "input_park": input_park,
            "reason": reason[0] if reason else None,
        }
    stalled = con.execute(
        "SELECT 1 FROM planner_alerts WHERE resolved_at IS NULL "
        "AND reason='wake_presend_retries_exhausted' "
        "AND (binding_id=? OR session_id=?) LIMIT 1",
        (binding_id, session_id)).fetchone()
    out["retry"] = {
        "applicable": bool(parked or input_park or stalled),
        "needs_outcome": input_park,
    }
    return out


def _binding_status(actor, query: dict):
    """GET /api/interface/sprint-bindings — the operator/planner wake-status
    surface (sprint 25 seq 10): a read-only projection of wake_state, the
    current batch, park/quarantine detail, and the last wake outcome per
    binding. A shell actor sees only its own planner bindings."""
    planner = _qint(query, "planner_shell_id")
    session_id = _qint(query, "session_id")
    doc_id = _qint(query, "sprint_doc_id")
    include_released = query.get("include_released", ["0"])[0] in (
        "1", "true", "yes")
    if actor.kind == "shell":
        planner = actor.shell_id
    sql = ("SELECT binding_id, sprint_doc_id, planner_shell_id, session_id,"
           " generation, armed_at, released_at, release_reason"
           " FROM sprint_planner_bindings")
    conds, params = [], []
    if planner is not None:
        conds.append("planner_shell_id=?")
        params.append(planner)
    if session_id is not None:
        conds.append("session_id=?")
        params.append(session_id)
    if doc_id is not None:
        conds.append("sprint_doc_id=?")
        params.append(doc_id)
    if not include_released:
        conds.append("released_at IS NULL")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY released_at IS NOT NULL, binding_id DESC LIMIT 20"
    con = _db()
    try:
        rows = con.execute(sql, params).fetchall()
        return _json(200, {"bindings":
                           [_project_binding(con, r) for r in rows]})
    finally:
        con.close()


def _sprint_alerts(actor, query: dict):
    """GET /api/interface/sprint-alerts — the operator's window into wake
    failures (spec Data Model planner_alerts; deduplicated while open).
    Default lists OPEN alerts; include_resolved=1 adds the audit history.
    A shell actor sees only alerts tied to its own sessions or bindings."""
    session_id = _qint(query, "session_id")
    binding_id = _qint(query, "binding_id")
    planner = _qint(query, "planner_shell_id")
    include_resolved = query.get("include_resolved", ["0"])[0] in (
        "1", "true", "yes")
    sql = ("SELECT alert_id, session_id, binding_id, message_id, watch_id,"
           " severity, reason, opened_at, resolved_at FROM planner_alerts")
    conds, params = [], []
    if actor.kind == "shell":
        conds.append(
            "(session_id IN (SELECT session_id FROM interface_sessions "
            "WHERE shell_id=?) OR binding_id IN (SELECT binding_id FROM "
            "sprint_planner_bindings WHERE planner_shell_id=?))")
        params += [actor.shell_id, actor.shell_id]
    elif planner is not None:
        conds.append(
            "(session_id IN (SELECT session_id FROM interface_sessions "
            "WHERE shell_id=?) OR binding_id IN (SELECT binding_id FROM "
            "sprint_planner_bindings WHERE planner_shell_id=?))")
        params += [planner, planner]
    if session_id is not None:
        conds.append("session_id=?")
        params.append(session_id)
    if binding_id is not None:
        conds.append("binding_id=?")
        params.append(binding_id)
    if not include_resolved:
        conds.append("resolved_at IS NULL")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY resolved_at IS NOT NULL, alert_id DESC LIMIT 100"
    con = _db()
    try:
        cols = ("alert_id", "session_id", "binding_id", "message_id",
                "watch_id", "severity", "reason", "opened_at", "resolved_at")
        alerts = [dict(zip(cols, r)) for r in con.execute(sql, params)]
        return _json(200, {"alerts": alerts})
    finally:
        con.close()


def _retry_binding(actor, headers, body, binding_id: int):
    """POST /api/interface/sprint-bindings/{id}/retry — the operator recovery
    path for parked/stalled wake work (spec Retry Policy, decision #22;
    sprint 25 seq 10). The parking invariant is law: a parked
    (delivery_unknown) batch is NEVER resubmitted — resolve_batch closes it
    as audit and returns its items to queued, an input park clears only on
    the operator's explicit delivered/not_delivered verdict, and the
    coordinator then forms a NEW batch and re-gates everything before a
    byte moves through the broker-owned writer."""
    con = _db()
    try:
        def produce():
            row = con.execute(
                "SELECT planner_shell_id, session_id, released_at "
                "FROM sprint_planner_bindings WHERE binding_id=?",
                (binding_id,)).fetchone()
            if row is None:
                return 404, _err_obj("no_such_binding",
                                     f"binding {binding_id} not found")
            planner, session_id, released = row
            if actor.kind == "shell" and actor.shell_id != planner:
                return 403, _err_obj(
                    "not_the_planner",
                    "a shell may retry only its own binding")
            if released is not None:
                return 409, _err_obj(
                    "binding_released",
                    f"binding {binding_id} is released — arm a fresh binding")
            actions = []
            input_reconciled = False
            inp = con.execute(
                "SELECT delivery FROM interface_input_state "
                "WHERE session_id=?", (session_id,)).fetchone()
            if inp is not None and inp[0] == "delivery_unknown":
                outcome = (body.get("outcome") or "").strip()
                if outcome not in ("delivered", "not_delivered"):
                    return 422, _err_obj(
                        "outcome_required",
                        "the session's input is parked delivery_unknown — "
                        "retry needs outcome=delivered|not_delivered")
                interface_broker.reconcile_input(con, session_id, outcome)
                input_reconciled = True
                actions.append(f"input park reconciled ({outcome})")
            # A parked batch is never the NEWEST batch for long — the drain
            # forms a new live batch once one parks (the common case: the
            # sprint keeps producing messages before the operator retries).
            # A newest-first pick would grab that newer batch, take the
            # re-signal branch, and strand the parked one's items 'batched'
            # forever (resolve_batch is the only requeue path). Resolve EVERY
            # parked batch: each closes as audit and its items requeue for a
            # NEW batch through the coordinator.
            parked = con.execute(
                "SELECT batch_id FROM planner_wake_batches "
                "WHERE binding_id=? AND state='delivery_unknown' "
                "ORDER BY batch_id", (binding_id,)).fetchall()
            for (parked_id,) in parked:
                interface_broker.resolve_batch(con, parked_id)
                actions.append(
                    f"parked batch {parked_id} resolved as audit — its "
                    "items requeue for a NEW batch")
            resignalled = False
            if con.execute(
                    "SELECT 1 FROM planner_wake_batches WHERE binding_id=? "
                    "AND state='queued' LIMIT 1", (binding_id,)).fetchone() \
                    or con.execute(
                    "SELECT 1 FROM planner_wake_items WHERE binding_id=? "
                    "AND state='queued' LIMIT 1", (binding_id,)).fetchone():
                actions.append(
                    "wake work re-signalled — the coordinator re-gates "
                    "from live state")
                resignalled = True
            elif not actions:
                return 409, _err_obj(
                    "nothing_to_retry",
                    "no input park, parked batch, or queued wake work on "
                    "this binding")
            # Clear ONLY the alerts this retry actually remedied — a blanket
            # clear would resolve a parked batch's alert while its items sat
            # stranded, making the park invisible to the operator
            # (dedupe-while-open re-arms any alert whose condition recurs).
            clears = []
            if parked:
                clears.append("wake_batch_delivery_unknown")
            if input_reconciled:
                clears.append("crash_window_delivery_unknown")
            if resignalled:
                clears.append("wake_presend_retries_exhausted")
            if clears:
                con.execute(
                    "UPDATE planner_alerts SET resolved_at=datetime('now') "
                    "WHERE resolved_at IS NULL "
                    f"AND reason IN ({','.join('?' * len(clears))}) "
                    "AND (binding_id=? OR session_id=?)",
                    (*clears, binding_id, session_id))
            con.commit()
            _log(f"sprint binding {binding_id} retry: {'; '.join(actions)}")
            return 200, {
                "binding_id": binding_id, "retried": True,
                "actions": actions,
                "wake_state": _wake_state(con, planner_shell_id=planner)}
        resp = _idempotent(con, actor, "sprint_binding_retry", headers,
                           body, produce)
        if resp[0] == 200:
            interface_wake.notify_binding(binding_id)
        return resp
    finally:
        con.close()


# ---------------------------------------------------------- action receipts

def _begin_receipt(actor, headers, body):
    """POST /api/planner-action-receipts — record action INTENT before a
    planner side effect (spec Event Ingress). The idempotency key derives
    from message + operation + target; an existing key returns the original
    receipt — a completed one suppresses the duplicate action outright."""
    message_id = body.get("message_id")
    operation = (body.get("operation") or "").strip()
    target = (body.get("target") or "").strip()
    if not operation or not target:
        return _err(422, "validation", "operation and target required")
    if message_id is not None and not isinstance(message_id, int):
        return _err(422, "validation", "message_id must be an int")
    idem_key = f"action|{message_id or '-'}|{operation}|{target}"
    con = _db()
    try:
        def produce():
            existing = con.execute(
                "SELECT receipt_id, state FROM planner_action_receipts "
                "WHERE idem_key=?", (idem_key,)).fetchone()
            if existing is not None:
                return 200, {"receipt_id": existing[0], "state": existing[1],
                             "duplicate": True,
                             "suppressed": existing[1] == "complete"}
            cur = con.execute(
                "INSERT INTO planner_action_receipts "
                "(message_id, operation, target, idem_key) VALUES (?,?,?,?)",
                (message_id, operation, target, idem_key))
            con.commit()
            return 201, {"receipt_id": cur.lastrowid, "state": "intent",
                         "idem_key": idem_key}
        return _idempotent(con, actor, "planner_action_begin", headers, body,
                           produce)
    finally:
        con.close()


def _update_receipt(actor, headers, body, receipt_id: int):
    """PATCH /api/planner-action-receipts/{id} — record the observed result:
    complete | unknown (parks the message's wake item for reconciliation on
    the next batch completion) | reconciled (operator-resolved unknown)."""
    new_state = body.get("state")
    if new_state not in ("complete", "unknown", "reconciled"):
        return _err(422, "validation",
                    "state must be complete | unknown | reconciled")
    detail = (body.get("result_detail") or "").strip() or None
    con = _db()
    try:
        def produce():
            extra = {"result_detail": detail}
            if new_state == "complete":
                extra["completed_at"] = _now(con)
            elif new_state == "reconciled":
                extra["reconciled_at"] = _now(con)
            try:
                interface_state.transition(
                    con, "receipt", receipt_id, new_state, extra_sets=extra)
            except interface_state.InterfaceTransitionError as exc:
                return 409, _err_obj("receipt_transition", str(exc))
            con.commit()
            return 200, {"receipt_id": receipt_id, "state": new_state}
        return _idempotent(con, actor, "planner_action_update", headers, body,
                           produce)
    finally:
        con.close()


def _hook_callback(headers, body):
    """Generation-scoped hook authority: the token calls ONLY this route, for
    its one generation. session_start additionally promotes the reservation
    (reserved → occupied) after exact identity proof.

    Contract (spec #20 Harness Hooks, seq 7): callbacks carry ONLY event,
    session, generation, sequence, PID identity, and token — no content.
    Wrong tokens, stale generations, replayed sequences, unknown events,
    illegal transitions, and PID mismatches are rejected and audited.
    `source` distinguishes the entrypoint's pre-exec identity claim from a
    provider-native hook; only the provider's session_start is readiness."""
    authz = headers.get("Authorization") or ""
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    shell_id, generation = body.get("shell_id"), body.get("generation")
    hook_seq, event = body.get("hook_seq"), body.get("event")
    source = body.get("source", "provider")
    if not token or not isinstance(shell_id, int) \
            or not isinstance(generation, int) \
            or not isinstance(hook_seq, int) or not event:
        _log(f"hook rejected (422 missing/invalid fields): "
             f"shell={shell_id} gen={generation} event={event!r}")
        return _err(422, "validation",
                    "bearer token + shell_id, generation, hook_seq, event")
    unknown = set(body) - {"shell_id", "generation", "hook_seq", "event",
                           "source", "pid", "start_ticks", "archive_id",
                           "cli_version"}
    if unknown:
        _log(f"hook rejected (422 unknown fields {sorted(unknown)}): "
             f"shell={shell_id} gen={generation} event={event!r}")
        return _err(422, "validation", f"unknown fields: {sorted(unknown)}")
    if event not in interface_hooks.EVENTS:
        _log(f"hook event rejected shell={shell_id} gen={generation} "
             f"event={event!r}")
        return _err(422, "validation", f"unknown hook event {event!r}")
    if source not in interface_hooks.SOURCES:
        _log(f"hook rejected (422 unknown source {source!r}): "
             f"shell={shell_id} gen={generation} event={event}")
        return _err(422, "validation", f"unknown hook source {source!r}")
    con = _db()
    try:
        gen = con.execute(
            "SELECT hook_token_hash, ended_at FROM interface_generations "
            "WHERE shell_id=? AND generation=?", (shell_id, generation)
        ).fetchone()
        if gen is None or \
                hashlib.sha256(token.encode()).hexdigest() != gen[0]:
            _log(f"hook auth rejected shell={shell_id} gen={generation} "
                 f"event={event}")
            return _err(403, "hook_auth",
                        "unknown generation or bad hook token")
        if gen[1] is not None:
            # The generation has ended. Every event is rejected EXCEPT a
            # session_end acknowledgement: a provider hook (its own end, or
            # one that lost the race to an operator close) gets a clean 200
            # — acknowledged, never reopened, never a rejection loop (#532).
            if event == "session_end":
                return _json(200, {"hook_seq": hook_seq, "event": event,
                                   "acknowledged": True,
                                   "already_ended": True})
            _log(f"hook rejected (ended generation) shell={shell_id} "
                 f"gen={generation} event={event}")
            return _err(403, "hook_auth",
                        "unknown generation or bad hook token")
        sess = con.execute(
            "SELECT session_id, occupancy, pane_pid FROM interface_sessions "
            "WHERE shell_id=? AND generation=? AND occupancy <> 'ended'",
            (shell_id, generation)).fetchone()
        if sess is None:
            _log(f"hook rejected (404 no live session): shell={shell_id} "
                 f"gen={generation} event={event}")
            return _err(404, "no_such_session", "no live session for generation")
        session_id, occupancy, pane_pid = sess
        try:
            # Exact identity (spec: PID presence is never authority): a
            # callback reporting a pid must report the pane's pid — the
            # exec chain makes harness pid == pane pid, and the emitter
            # passes $PPID. session_start MUST carry it (entrypoint and
            # emitter both do); any mismatch fails closed, on any event.
            if body.get("pid") is None and event == "session_start":
                _log(f"hook rejected (422 session_start without pid): "
                     f"session={session_id} source={source}")
                return _err(422, "validation",
                            "session_start requires pid identity")
            if body.get("pid") is not None and body.get("pid") != pane_pid:
                _log(f"hook identity mismatch session={session_id} "
                     f"event={event} pid={body.get('pid')} "
                     f"pane_pid={pane_pid}")
                return _err(403, "identity_mismatch",
                            "reported pid is not the pane's pid")
            if event == "session_start":
                if source == "entrypoint" and occupancy == "reserved":
                    interface_state.transition(
                        con, "occupancy", session_id, "occupied",
                        extra_sets={"occupied_at": _now(con),
                                    "archive_id": body.get("archive_id"),
                                    "harness_pid": body.get("pid"),
                                    "harness_start_ticks":
                                        body.get("start_ticks"),
                                    "cli_version": body.get("cli_version")})
            result = interface_broker.record_hook(
                con, shell_id, generation, hook_seq, event, source=source)
            con.commit()
            # A lifecycle event may make queued wake work submittable
            # (turn_stop → idle, provider session_start → ready+quiet
            # baseline). Signal after commit; a no-op when nothing is queued.
            interface_wake.notify_session(session_id)
            return _json(200, result)
        except interface_broker.BrokerError as exc:
            # Flag #51 (decision #31): EVERY rejection is audited — a silent
            # rejection hides exactly the losses #50's ordering needs to
            # diagnose in production (stale/replayed hook_seq among them).
            _log(f"hook rejected (409 {event} shell={shell_id} "
                 f"gen={generation} seq={hook_seq}): {exc}")
            return _err(409, "hook_rejected", str(exc))
        except interface_state.InterfaceTransitionError as exc:
            _log(f"hook illegal transition session={session_id} "
                 f"event={event}: {exc}")
            return _err(409, "hook_rejected", f"illegal transition: {exc}")
    finally:
        con.close()


def _on_unexpected_exit(session_id: int) -> None:
    """Runtime callback: the pane died with no operator termination. Verified
    exit moves lifecycle → lost and occupancy → unreconciled (spec Occupancy
    Model); New chat stays blocked until reconcile/close."""
    con = _db()
    try:
        sess = con.execute(
            "SELECT occupancy, lifecycle FROM interface_sessions "
            "WHERE session_id=?", (session_id,)).fetchone()
        if sess is None or sess[0] != "occupied":
            return
        interface_state.transition(con, "lifecycle", session_id, "lost")
        interface_state.transition(con, "occupancy", session_id,
                                   "unreconciled",
                                   extra_sets={"error_detail":
                                               "pane exited unexpectedly"})
        con.execute(
            "INSERT OR IGNORE INTO planner_alerts "
            "(session_id, severity, reason, dedupe_key) "
            "VALUES (?, 'critical', 'session_lost', ?)",
            (session_id, f"{session_id}|-|session_lost"))
        con.commit()
        _log(f"session {session_id}: unexpected exit → lost/unreconciled")
    except Exception as exc:  # noqa: BLE001
        _log(f"on_unexpected_exit({session_id}) failed: {exc!r}")
    finally:
        con.close()


# ------------------------------------------------------------------ bootstrap

def _browser_session(headers, body):
    """Bootstrap = an EXCHANGE (spec #20 API Resources, decision on flag
    #43): the caller presents the mode-0600 operator capability and gets an
    HttpOnly SameSite=Strict browser session back. Same-origin proof alone
    mints NOTHING — without the capability any local process could
    self-mint operator-equivalent authority."""
    if not _same_origin_proof(headers):
        return _err(403, "not_same_origin",
                    "browser sessions mint only from the same-origin UI")
    authz = headers.get("Authorization") or ""
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    if not token or token != _operator_token():
        return _err(401, "operator_capability_required",
                    "browser bootstrap exchanges the operator capability "
                    "(Authorization: Bearer <operator token>)")
    if not (headers.get("Idempotency-Key") or ""):
        return _err(422, "idempotency_key_required",
                    "Idempotency-Key header is required for Interface mutations")
    token = secrets.token_hex(24)
    csrf = secrets.token_hex(24)
    with _browser_lock:
        _browser_sessions[token] = {"csrf": csrf, "created": time.time()}
    cookie = (f"sc_if={token}; HttpOnly; SameSite=Strict; Path=/")
    return _json(201, {"csrf": csrf}, headers=[("Set-Cookie", cookie)])


# ------------------------------------------------------------------ dispatch

class _BadPathId(ValueError):
    """A route-shaped path whose identifier segment is not an int."""


def _path_id(path: str, segment: int = -1) -> int:
    """Parse one path segment as a route identifier; a bad segment is a
    422 parsing error, never a 404 no_such_route (spec #30 req 4)."""
    try:
        return int(path.split("/")[segment])
    except (ValueError, IndexError):
        raise _BadPathId(f"invalid path identifier: {path!r}")


def handle(method: str, path: str, headers_raw: str, body: bytes) -> tuple:
    from urllib.parse import parse_qs, urlparse
    headers = _parse_headers(headers_raw)
    if not _host_ok(headers):
        return _err(403, "bad_host", "Interface API serves 127.0.0.1/localhost only")
    u = urlparse(path)
    p = u.path
    query = parse_qs(u.query)
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return _err(400, "bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        return _err(400, "bad_json", "request body must be a JSON object")

    # Bootstrap and hook callbacks have their own authority models.
    if p == "/api/interface/browser-sessions" and method == "POST":
        return _browser_session(headers, data)
    if p == "/api/interface/hook-callbacks" and method == "POST":
        return _hook_callback(headers, data)

    actor = _resolve_actor(headers)
    if actor is None:
        return _err(401, "unauthorized",
                    "operator bearer or browser session required")
    # Shell actors (the planner's own API token) reach ONLY the sprint-wake
    # surfaces — bindings + wake ops + action receipts — never
    # session/writer/stop.
    if actor.kind == "shell" and not (
            p.startswith("/api/interface/sprint-bindings")
            or p.startswith("/api/interface/sprint-alerts")
            or p.startswith("/api/planner-action-receipts")):
        return _err(403, "shell_scope",
                    "a shell token may call only sprint-binding, "
                    "sprint-alert, and action-receipt routes")
    if method in ("POST", "DELETE", "PATCH", "PUT"):
        if not actor.csrf_ok:
            return _err(403, "csrf", "browser mutations need the session's "
                                     "anti-forgery token (X-CSRF)")
        if not _mutation_site_ok(headers):
            return _err(403, "not_same_origin",
                        "cross-site mutation rejected")

    try:
        if p == "/api/interface/shells" and method == "GET":
            return _list_shells()
        if p == "/api/interface/sessions" and method == "POST":
            return _create_session(actor, headers, data)
        if p.startswith("/api/interface/sessions/") and method == "GET":
            return _get_session(_path_id(p))
        if p == "/api/interface/stream-tickets" and method == "POST":
            return _mint_ticket(actor, headers, data)
        if p == "/api/interface/writer-leases" and method == "POST":
            return _acquire_lease(actor, headers, data)
        if p.startswith("/api/interface/writer-leases/") and method == "DELETE":
            return _release_lease(actor, headers, data, _path_id(p))
        if p == "/api/interface/clean-certifications" and method == "POST":
            return _certify_clean(actor, headers, data)
        if p == "/api/interface/termination-requests" and method == "POST":
            return _terminate(actor, headers, data)
        if p == "/api/interface/reconciliations" and method == "POST":
            return _reconcile(actor, headers, data)
        if p == "/api/interface/sprint-bindings" and method == "POST":
            return _arm_binding(actor, headers, data)
        if p == "/api/interface/sprint-bindings" and method == "GET":
            return _binding_status(actor, query)
        if p == "/api/interface/sprint-alerts" and method == "GET":
            return _sprint_alerts(actor, query)
        if p.startswith("/api/interface/sprint-bindings/") \
                and p.endswith("/retry") and method == "POST":
            return _retry_binding(actor, headers, data, _path_id(p, -2))
        if p.startswith("/api/interface/sprint-bindings/") \
                and method == "DELETE":
            return _release_binding(actor, headers, data, _path_id(p))
        if p == "/api/planner-action-receipts" and method == "POST":
            return _begin_receipt(actor, headers, data)
        if p.startswith("/api/planner-action-receipts/") and method == "PATCH":
            return _update_receipt(actor, headers, data, _path_id(p))
        return _err(404, "no_such_route", f"no route: {method} {p}")
    except _BadPathId as exc:
        # Route PARSING error — a legal route shape with a bad identifier.
        return _err(422, "invalid_path_id", str(exc))
    except (interface_state.InterfaceTransitionError,
            interface_broker.BrokerError) as exc:
        # A legal route whose durable state refused the transition. NEVER
        # no_such_route (#523): the old broad `except ValueError` rewrote
        # these internal state conflicts to a false 404.
        _log(f"state conflict {method} {p}: {exc}")
        return _err(409, "state_conflict", str(exc))
    except Exception as exc:  # noqa: BLE001 — the last-resort boundary
        # Unexpected handler failure: sanitized 500 with a server-side
        # correlation record; internals never cross the wire.
        correlation = secrets.token_hex(8)
        _log(f"handler failure {method} {p} correlation={correlation}: "
             f"{exc!r}\n{traceback.format_exc()}")
        return _err(500, "internal",
                    "unexpected server failure — quote correlation "
                    f"{correlation} when reporting",
                    {"correlation": correlation})
