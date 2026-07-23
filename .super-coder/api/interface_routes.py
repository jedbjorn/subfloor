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
import interface_state  # noqa: E402
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
    def __init__(self, kind: str, scope: str, csrf_ok: bool):
        self.kind = kind          # "operator" | "browser"
        self.scope = scope        # idempotency actor_scope
        self.csrf_ok = csrf_ok    # mutation authority for browser actors


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

def _worktree_for(shortname: str) -> str:
    return str(REPO_ROOT / ".sc-worktrees" / shortname.lower())


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
            "SELECT shell_id, shortname FROM shells "
            "WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
            (shell_id,)).fetchone()
        if shell is None:
            return _err(404, "no_such_shell", f"shell {shell_id} not found")
        shortname = shell[1]

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
                 _worktree_for(shortname), f"+{RESERVATION_TTL_S} seconds"))
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
                "worktree": _worktree_for(shortname),
                "harness": harness, "model": body.get("model"),
                "effort": body.get("effort")}))
            os.chmod(token_path, 0o600)
            try:
                identity = _runtime.call(_runtime.spawn(
                    session_id=session_id, shell_id=shell_id,
                    generation=gen_no, worktree=_worktree_for(shortname),
                    sc_path=str(REPO_ROOT / "sc"),
                    token_path=str(token_path), rows=rows, cols=cols))
            except Exception as exc:  # noqa: BLE001 — see below
                # Definite pre-spawn failure (runtime down, bad worktree)
                # closes the reservation; an AMBIGUOUS tmux outcome leaves it
                # unreconciled (spec #20 Interface Workflow 4) — never a
                # second process, never an auto-kill.
                from interface_runtime import InterfaceUnavailable
                definite = isinstance(exc, (InterfaceUnavailable,
                                            FileNotFoundError, ValueError))
                interface_state.transition(
                    con, "occupancy", session_id,
                    "ended" if definite else "unreconciled",
                    extra_sets={"error_detail": f"spawn failed: {exc}"[:400],
                                **({"ended_at": _now(con),
                                    "end_reason": "spawn_failed"}
                                   if definite else {})})
                if definite:
                    con.execute(
                        "UPDATE interface_generations SET "
                        "ended_at=datetime('now') "
                        "WHERE shell_id=? AND generation=?",
                        (shell_id, gen_no))
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
            # existing owner, never a second process).
            owner = con.execute(
                "SELECT session_id FROM interface_sessions "
                "WHERE shell_id=? AND occupancy <> 'ended'",
                (shell_id,)).fetchone()
            return _err(409, "shell_occupied",
                        "a concurrent start owns this shell",
                        {"session_id": owner[0] if owner else None})
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
            "wake_state": "disarmed",
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
                        "wake_state": "disarmed", **proj})
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
        if sess[2] != "occupied":
            return _err(409, "not_occupied",
                        f"session {session_id} is {sess[2]}, not occupied")
        if force and sess[4] is None:
            # Spec Workflow 9: force only AFTER graceful termination fails
            # and shows the PID/generation it will end. The UI sequences it;
            # the API (authority surface for the seq-6 CLI) enforces it.
            return _err(409, "force_requires_graceful_timeout",
                        "force is available only after a graceful "
                        "termination timed out for this session")

        def produce():
            interface_state.transition(con, "lifecycle", session_id, "stopping")
            con.commit()
            result = _runtime.call(_runtime.terminate(session_id, force=force))
            if not result.get("terminated"):
                if result.get("reason") == "identity_mismatch":
                    # Fail closed (spec: never kill an uncertain process) —
                    # the session is now uncertain, not endable.
                    interface_state.transition(con, "occupancy", session_id,
                                               "unreconciled")
                    interface_state.transition(con, "lifecycle", session_id,
                                               "lost")
                    con.commit()
                    return 409, {"terminated": False,
                                 "reason": "identity_mismatch"}
                # graceful timeout: stays stopping, and the timeout is
                # recorded durably — it is what unlocks the force follow-up.
                interface_state.transition(
                    con, "lifecycle", session_id, "stopping",
                    extra_sets={"graceful_timed_out_at": _now(con)})
                con.commit()
                return 200, {"terminated": False,
                             "reason": result.get("reason", "graceful_timeout"),
                             "pid": result.get("pid"),
                             "generation": result.get("generation")}
            _end_session(con, session_id,
                         "operator_force" if force else "operator_end")
            con.commit()
            return 202, {"terminated": True}

        return _idempotent(con, actor, "terminate", headers, body, produce)
    finally:
        con.close()


def _end_session(con, session_id: int, end_reason: str) -> None:
    """Durable closure: occupancy → ended (availability derives), generation
    ended, leases revoked. The shell offers New chat only after this."""
    row = con.execute(
        "SELECT shell_id, generation FROM interface_sessions "
        "WHERE session_id=?", (session_id,)).fetchone()
    interface_state.transition(
        con, "occupancy", session_id, "ended",
        extra_sets={"ended_at": _now(con), "end_reason": end_reason})
    interface_state.transition(con, "lifecycle", session_id, "ended")
    con.execute(
        "UPDATE interface_generations SET ended_at=datetime('now') "
        "WHERE shell_id=? AND generation=? AND ended_at IS NULL",
        (row[0], row[1]))
    con.execute(
        "UPDATE interface_writer_leases SET revoked_at=datetime('now'), "
        "revoke_reason='session_end' "
        "WHERE session_id=? AND revoked_at IS NULL", (session_id,))


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
                _end_session(con, session_id, "operator_close")
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

def _hook_callback(headers, body):
    """Generation-scoped hook authority: the token calls ONLY this route, for
    its one generation. session_start additionally promotes the reservation
    (reserved → occupied) after exact identity proof."""
    authz = headers.get("Authorization") or ""
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    shell_id, generation = body.get("shell_id"), body.get("generation")
    hook_seq, event = body.get("hook_seq"), body.get("event")
    if not token or not isinstance(shell_id, int) \
            or not isinstance(generation, int) \
            or not isinstance(hook_seq, int) or not event:
        return _err(422, "validation",
                    "bearer token + shell_id, generation, hook_seq, event")
    con = _db()
    try:
        gen = con.execute(
            "SELECT hook_token_hash, ended_at FROM interface_generations "
            "WHERE shell_id=? AND generation=?", (shell_id, generation)
        ).fetchone()
        if gen is None or gen[1] is not None or \
                hashlib.sha256(token.encode()).hexdigest() != gen[0]:
            _log(f"hook auth rejected shell={shell_id} gen={generation} "
                 f"event={event}")
            return _err(403, "hook_auth",
                        "unknown generation or bad hook token")
        sess = con.execute(
            "SELECT session_id, occupancy, pane_pid FROM interface_sessions "
            "WHERE shell_id=? AND generation=? AND occupancy <> 'ended'",
            (shell_id, generation)).fetchone()
        if sess is None:
            return _err(404, "no_such_session", "no live session for generation")
        session_id, occupancy, pane_pid = sess
        try:
            if event == "session_start":
                # Exact identity (spec: PID presence is never authority): the
                # entrypoint's pid must be the pane's pid — exec-chained all
                # the way down. Any mismatch fails closed.
                if body.get("pid") != pane_pid:
                    _log(f"session_start identity mismatch session="
                         f"{session_id} pid={body.get('pid')} "
                         f"pane_pid={pane_pid}")
                    return _err(403, "identity_mismatch",
                                "reported pid is not the pane's pid")
                if occupancy == "reserved":
                    interface_state.transition(
                        con, "occupancy", session_id, "occupied",
                        extra_sets={"occupied_at": _now(con),
                                    "archive_id": body.get("archive_id"),
                                    "harness_pid": body.get("pid"),
                                    "harness_start_ticks":
                                        body.get("start_ticks"),
                                    "cli_version": body.get("cli_version")})
                result = interface_broker.record_hook(
                    con, shell_id, generation, hook_seq, event)
            else:
                result = interface_broker.record_hook(
                    con, shell_id, generation, hook_seq, event)
            con.commit()
            return _json(200, result)
        except interface_broker.BrokerError as exc:
            return _err(409, "hook_rejected", str(exc))
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

def handle(method: str, path: str, headers_raw: str, body: bytes) -> tuple:
    from urllib.parse import urlparse
    headers = _parse_headers(headers_raw)
    if not _host_ok(headers):
        return _err(403, "bad_host", "Interface API serves 127.0.0.1/localhost only")
    p = urlparse(path).path
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
            return _get_session(int(p.rsplit("/", 1)[1]))
        if p == "/api/interface/stream-tickets" and method == "POST":
            return _mint_ticket(actor, headers, data)
        if p == "/api/interface/writer-leases" and method == "POST":
            return _acquire_lease(actor, headers, data)
        if p.startswith("/api/interface/writer-leases/") and method == "DELETE":
            return _release_lease(actor, headers, data,
                                  int(p.rsplit("/", 1)[1]))
        if p == "/api/interface/clean-certifications" and method == "POST":
            return _certify_clean(actor, headers, data)
        if p == "/api/interface/termination-requests" and method == "POST":
            return _terminate(actor, headers, data)
        if p == "/api/interface/reconciliations" and method == "POST":
            return _reconcile(actor, headers, data)
        return _err(404, "no_such_route", f"no route: {method} {p}")
    except ValueError:
        return _err(404, "no_such_route", f"no route: {method} {p}")
