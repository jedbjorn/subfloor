"""Conductor contract HTTP API.

Routes:
  GET  /api/directives[?status=&kind=&sprint_doc_id=&limit=]
  GET  /api/directives/{id}
  POST /api/directives
  GET  /api/sentinel-events[?event_kind=&sprint_doc_id=&unit_id=&limit=]
  GET  /api/sentinel-events/{id}

Directive creation is token-scoped: the bearer token, never request data,
selects the issuer shell and flavor. Sentinel writes are engine-internal; the
HTTP surface is read-only until the sentinel lands in Step 5.
"""
from __future__ import annotations

import http.client
import io
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"
sys.path.insert(0, str(ENGINE / "scripts"))
import conductor_runtime  # noqa: E402
import db_driver  # noqa: E402
import sprint_lifecycle  # noqa: E402

_ALLOWED_HOST_SET = frozenset(("127.0.0.1", "localhost", "::1"))
_STATUSES = frozenset(("pending", "executed", "refused"))


def _db():
    return db_driver.connect(str(DB_PATH))


def _json(status: int, obj):
    return status, [
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
    ], json.dumps(obj).encode()


def _err(status: int, code: str, message: str):
    return _json(status, {"error": {"code": code, "message": message}})


def _headers(raw: str):
    return http.client.parse_headers(io.BytesIO(raw.encode("latin-1")))


def _host_ok(headers) -> bool:
    host = (headers.get("Host") or "").strip()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in _ALLOWED_HOST_SET


def _shell_for_token(con, headers):
    authz = headers.get("Authorization") or ""
    if authz[:7].lower() != "bearer ":
        return None
    token = authz[7:].strip()
    if not token:
        return None
    return con.execute(
        "SELECT shell_id, flavor FROM shells "
        "WHERE api_key=? AND COALESCE(is_deleted,0)=0",
        (token,),
    ).fetchone()


def _int(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None


def _limit(query) -> int:
    raw = query.get("limit", ["50"])[0]
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("limit must be an integer") from None
    if not 1 <= value <= 200:
        raise ValueError("limit must be between 1 and 200")
    return value


def _decode(row, json_column: str):
    out = dict(row)
    out[json_column] = json.loads(out[json_column])
    return out


def _directive(con, directive_id: int):
    row = con.execute(
        "SELECT directive_id, issuer_shell_id, issuer_flavor, kind, payload, "
        "target, sprint_doc_id, unit_id, status, refusal_reason, created_at, "
        "executed_at FROM directives WHERE directive_id=?",
        (directive_id,),
    ).fetchone()
    return _decode(row, "payload") if row is not None else None


def _event(con, event_id: int):
    row = con.execute(
        "SELECT event_id, event_kind, shell_id, sprint_doc_id, unit_id, "
        "directive_id, evidence, observed_at "
        "FROM sentinel_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return _decode(row, "evidence") if row is not None else None


def insert_system_directive(con, *, kind: str, target: str,
                            payload=None, sprint_doc_id=None, unit_id=None):
    """Engine-internal system directive writer for the Step 5 sentinel."""
    cur = con.execute(
        "INSERT INTO directives "
        "(issuer_shell_id, issuer_flavor, kind, payload, target, "
        " sprint_doc_id, unit_id) VALUES (NULL,'system',?,?,?,?,?)",
        (kind, json.dumps(payload or {}, sort_keys=True), target,
         sprint_doc_id, unit_id),
    )
    return cur.lastrowid


def append_sentinel_event(con, *, event_kind: str, evidence=None,
                          shell_id=None, sprint_doc_id=None, unit_id=None,
                          directive_id=None):
    """Engine-internal append seam for the Step 5 sentinel."""
    cur = con.execute(
        "INSERT INTO sentinel_events "
        "(event_kind, shell_id, sprint_doc_id, unit_id, directive_id, evidence) "
        "VALUES (?,?,?,?,?,?)",
        (event_kind, shell_id, sprint_doc_id, unit_id, directive_id,
         json.dumps(evidence or {}, sort_keys=True)),
    )
    return cur.lastrowid


def _list_directives(con, query):
    clauses, params = [], []
    status = query.get("status", [None])[0]
    if status:
        if status not in _STATUSES:
            raise ValueError("status must be pending, executed, or refused")
        clauses.append("status=?")
        params.append(status)
    kind = query.get("kind", [None])[0]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    sprint = query.get("sprint_doc_id", [None])[0]
    if sprint not in (None, ""):
        clauses.append("sprint_doc_id=?")
        params.append(_int(sprint, "sprint_doc_id"))
    sql = (
        "SELECT directive_id, issuer_shell_id, issuer_flavor, kind, payload, "
        "target, sprint_doc_id, unit_id, status, refusal_reason, created_at, "
        "executed_at FROM directives"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY directive_id DESC LIMIT ?"
    params.append(_limit(query))
    return [_decode(r, "payload") for r in con.execute(sql, params)]


def _list_events(con, query):
    clauses, params = [], []
    kind = query.get("event_kind", [None])[0]
    if kind:
        clauses.append("event_kind=?")
        params.append(kind)
    for field in ("sprint_doc_id", "unit_id"):
        raw = query.get(field, [None])[0]
        if raw not in (None, ""):
            clauses.append(f"{field}=?")
            params.append(_int(raw, field))
    sql = (
        "SELECT event_id, event_kind, shell_id, sprint_doc_id, unit_id, "
        "directive_id, evidence, observed_at FROM sentinel_events"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY event_id DESC LIMIT ?"
    params.append(_limit(query))
    return [_decode(r, "evidence") for r in con.execute(sql, params)]


def _create_directive(con, headers, body):
    allowed_fields = {
        "issuer_shell_id", "issuer_flavor", "kind", "payload", "target",
        "sprint_doc_id", "unit_id",
    }
    unknown = sorted(set(body) - allowed_fields)
    if unknown:
        return _err(
            422, "validation",
            "unknown directive field(s): " + ", ".join(unknown),
        )
    shell = _shell_for_token(con, headers)
    if shell is None:
        return _err(
            401, "issuer_required",
            "directive creation requires a valid shell bearer token",
        )
    shell_id, flavor = shell
    if flavor not in ("dev", "reviewer", "planner"):
        return _err(403, "issuer_flavor_invalid",
                    f"shell {shell_id} has no directive-issuing flavor")
    if "issuer_shell_id" in body and body["issuer_shell_id"] != shell_id:
        return _err(422, "issuer_claim_mismatch",
                    "issuer_shell_id is derived from the bearer token")
    if "issuer_flavor" in body and body["issuer_flavor"] != flavor:
        return _err(422, "issuer_claim_mismatch",
                    "issuer_flavor is derived from the bearer token")
    kind = body.get("kind")
    target = body.get("target")
    payload = body.get("payload", {})
    if not isinstance(kind, str) or not kind.strip():
        return _err(422, "validation", "kind must be a nonblank string")
    if not isinstance(target, str) or not target.strip():
        return _err(422, "validation", "target must be a nonblank string")
    if not isinstance(payload, dict):
        return _err(422, "validation", "payload must be a JSON object")
    allowed = con.execute(
        "SELECT 1 FROM directive_kinds WHERE issuer_flavor=? AND kind=?",
        (flavor, kind),
    ).fetchone()
    if allowed is None:
        return _err(
            422, "directive_kind_not_allowed",
            f"{flavor} may not issue {kind!r}",
        )
    if flavor == "planner" and kind == "handoff":
        return _err(
            409,
            "handoff_retired",
            "Planner handoff is retired; stage the board, then arm it with "
            "`sc sprint arm --sprint <id>`",
        )
    try:
        sprint = (
            _int(body["sprint_doc_id"], "sprint_doc_id")
            if body.get("sprint_doc_id") is not None else None
        )
        unit = (
            _int(body["unit_id"], "unit_id")
            if body.get("unit_id") is not None else None
        )
    except ValueError as exc:
        return _err(422, "validation", str(exc))
    if unit is not None:
        linked = con.execute(
            "SELECT 1 FROM sprint_units "
            "WHERE unit_id=? AND sprint_doc_id=?",
            (unit, sprint),
        ).fetchone()
        if linked is None:
            return _err(422, "unit_sprint_mismatch",
                        "unit_id does not belong to sprint_doc_id")
    if flavor == "planner" and sprint is not None:
        try:
            owner = sprint_lifecycle.planner_for_sprint(con, sprint)
        except sprint_lifecycle.SprintLifecycleError as exc:
            return _err(409, "sprint_owner_required", str(exc))
        if owner["shell_id"] != shell_id:
            return _err(
                403,
                "not_sprint_owner",
                f"shell {shell_id} is not sprint {sprint}'s originating Planner",
            )
    try:
        cur = con.execute(
            "INSERT INTO directives "
            "(issuer_shell_id, issuer_flavor, kind, payload, target, "
            " sprint_doc_id, unit_id) VALUES (?,?,?,?,?,?,?)",
            (shell_id, flavor, kind, json.dumps(payload, sort_keys=True),
             target.strip(), sprint, unit),
        )
        con.commit()
    except db_driver.IntegrityError as exc:
        con.rollback()
        return _err(422, "directive_invalid", str(exc))
    item = _directive(con, cur.lastrowid)
    conductor_runtime.maybe_wake(con)
    return _json(201, item)


def _act_directive(con, headers, directive_id: int):
    shell = _shell_for_token(con, headers)
    if shell is None:
        return _err(
            401, "conductor_required",
            "directive execution requires a valid conductor bearer token",
        )
    shell_id, flavor = shell
    if flavor != "conductor":
        return _err(
            403, "conductor_required",
            f"shell {shell_id} is {flavor or 'bespoke'}, not conductor",
        )
    try:
        result = conductor_runtime.act(con, directive_id, shell_id)
    except KeyError:
        return _err(404, "not_found", "no such directive")
    except PermissionError as exc:
        return _err(403, "conductor_required", str(exc))
    return _json(200, result)


def handle(method: str, path: str, headers_raw: str, body: bytes):
    headers = _headers(headers_raw)
    if not _host_ok(headers):
        return _err(403, "host_not_allowed",
                    "Conductor API serves 127.0.0.1/localhost only")
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    parts = parsed.path.strip("/").split("/")
    con = _db()
    try:
        if parts[:2] == ["api", "directives"]:
            if len(parts) == 2 and method == "GET":
                try:
                    return _json(200, {
                        "directives": _list_directives(con, query)
                    })
                except ValueError as exc:
                    return _err(422, "validation", str(exc))
            if len(parts) == 2 and method == "POST":
                try:
                    data = json.loads(body) if body else {}
                except ValueError:
                    return _err(400, "bad_json",
                                "request body is not valid JSON")
                if not isinstance(data, dict):
                    return _err(400, "bad_json",
                                "request body must be a JSON object")
                return _create_directive(con, headers, data)
            if len(parts) == 3 and method == "GET":
                try:
                    item = _directive(con, _int(parts[2], "directive_id"))
                except ValueError as exc:
                    return _err(422, "validation", str(exc))
                return _json(200, item) if item else _err(
                    404, "not_found", "no such directive")
            if len(parts) == 4 and parts[3] == "act" and method == "POST":
                try:
                    directive_id = _int(parts[2], "directive_id")
                except ValueError as exc:
                    return _err(422, "validation", str(exc))
                return _act_directive(con, headers, directive_id)
        if parts[:2] == ["api", "sentinel-events"]:
            if len(parts) == 2 and method == "GET":
                try:
                    return _json(200, {
                        "events": _list_events(con, query)
                    })
                except ValueError as exc:
                    return _err(422, "validation", str(exc))
            if len(parts) == 3 and method == "GET":
                try:
                    item = _event(con, _int(parts[2], "event_id"))
                except ValueError as exc:
                    return _err(422, "validation", str(exc))
                return _json(200, item) if item else _err(
                    404, "not_found", "no such sentinel event")
        return _err(404, "no_such_route",
                    f"no route: {method} {parsed.path}")
    finally:
        con.close()
