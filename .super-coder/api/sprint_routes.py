"""Sprint board HTTP API — /api/sprint-units (spec #20 board-as-record).

Extracted from the retired Interface stack (conductor Step 1): the DB-driven
board is a keeper; the Interface's browser-session/CSRF authority model died
with the Interface. Authority here matches the sibling server routes:

- a Bearer token resolving to `shells.api_key` is a SHELL actor — every
  participant reads the board; writes are fenced per sprint by
  `_may_write_board` (planner-only, FnB directive);
- no token on the localhost-fenced server is the OPERATOR (the review GUI's
  reads and host-side curl) — the operator owns everything, exactly as the
  rest of this API treats the local machine (spec #26: the hostile web origin
  is the boundary, not the local user).

Every mutation requires Idempotency-Key: an exact retry replays the stored
response, a key reused with a different body returns 409
(interface_idempotency_keys — table retained; renaming is Step 4's call).

Runs on the transport's executor threads (blocking sqlite, per-request
connections), dispatched from server.py.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402
import conductor_policy  # noqa: E402
import conductor_runtime  # noqa: E402
import conversation_broker  # noqa: E402
import conversation_events  # noqa: E402
import run as run_mod  # noqa: E402
import sprint_conversations  # noqa: E402
import sprint_lifecycle  # noqa: E402
import sprint_state  # noqa: E402
from sprint_units import SPRINT_UNIT_EDGES  # noqa: E402
from sprint_units import SprintTransitionError  # noqa: E402
from sprint_units import TERMINAL_UNIT_STATES as _TERMINAL_UNIT_STATES  # noqa: E402,E501
from sprint_units import UNIT_STATES as _UNIT_STATES  # noqa: E402
from sprint_units import check_transition as _check_transition  # noqa: E402

IDEM_TTL_S = 24 * 3600
_ALLOWED_HOST_SET = frozenset(("127.0.0.1", "localhost", "::1"))


def _log(msg: str) -> None:
    print(f"[sprint-api {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr,
          flush=True)


def _db():
    return db_driver.connect(str(DB_PATH))


def _json(status: int, obj, headers=None):
    body = json.dumps(obj).encode()
    # Board responses are live state — no-store, stated once at the sole
    # constructor rather than per-route where it would drift.
    hdrs = [("Content-Type", "application/json"),
            ("Cache-Control", "no-store")] + list(headers or [])
    return status, hdrs, body


def _err(status: int, code: str, message: str, details=None, headers=None):
    return _json(status, {"error": {"code": code, "message": message,
                                    "details": details or {}}}, headers)


def _err_obj(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message,
                      "details": details or {}}}


def _parse_headers(headers_raw: str):
    return http.client.parse_headers(io.BytesIO(headers_raw.encode("latin-1")))


def _host_ok(headers) -> bool:
    """Reject a Host outside 127.0.0.1/localhost (DNS rebind fence)."""
    host = (headers.get("Host") or "").strip()
    if host.startswith("["):                       # [::1]:port
        host = host[1:].split("]", 1)[0]
    else:
        host = host.rsplit(":", 1)[0] if ":" in host else host
    return host in _ALLOWED_HOST_SET


def _same_origin_as_host(origin: str, host: str) -> bool:
    """A browser Origin must name this request's exact HTTP(S) authority."""
    parsed = urlparse(origin)
    return (
        parsed.scheme in ("http", "https")
        and not (
            parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
        )
        and parsed.netloc == host
    )


def _mutation_site_ok(headers) -> bool:
    """Keep hostile web origins outside the no-token operator boundary.

    Host fencing stops DNS rebinding, but it does not stop a foreign page
    from sending a request directly to 127.0.0.1. Origin and Fetch Metadata
    are browser-controlled, so shell curl/API calls may omit them while a
    browser mutation must prove it is same-origin.
    """
    origin = headers.get("Origin")
    if origin and not _same_origin_as_host(
            origin, headers.get("Host") or ""):
        return False
    return headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")


# ------------------------------------------------------------------ authority

class _Actor:
    def __init__(self, kind: str, scope: str,
                 shell_id: "int | None" = None):
        self.kind = kind          # "operator" | "shell"
        self.scope = scope        # idempotency actor_scope
        self.shell_id = shell_id  # set for kind="shell"


def _resolve_actor(headers) -> "_Actor | None":
    """Bearer → shell actor; no token → operator (localhost is the operator —
    same stance as the sibling server routes). A PRESENTED token that matches
    nothing is refused rather than downgraded: a shell with a stale key must
    hear 401, not act as the operator."""
    authz = headers.get("Authorization") or ""
    if authz[:7].lower() == "bearer ":
        token = authz[7:].strip()
        if not token:
            return None
        con = _db()
        try:
            row = con.execute(
                "SELECT shell_id FROM shells WHERE api_key=? "
                "AND COALESCE(is_deleted,0)=0", (token,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return _Actor("shell", f"shell:{row[0]}", shell_id=row[0])
    return _Actor("operator", "operator")


# ------------------------------------------------------------------ idempotency

def _idempotent(con, actor: _Actor, operation: str, headers, body_obj,
                produce):
    """Idempotency-Key discipline for every board mutation: missing key →
    422; exact replay → the original response; key + different body → 409.
    `produce()` returns (status, obj) and runs ONLY on a fresh key."""
    key = headers.get("Idempotency-Key") or ""
    if not key:
        return _err(422, "idempotency_key_required",
                    "Idempotency-Key header is required for board mutations")
    canonical = hashlib.sha256(
        json.dumps(body_obj, sort_keys=True, default=str).encode()).hexdigest()
    # Expiry is part of the contract: remove stale records before lookup so an
    # old key no longer shadows a fresh mutation.
    expired = con.execute(
        "DELETE FROM interface_idempotency_keys "
        "WHERE expires_at <= datetime('now')").rowcount
    row = con.execute(
        "SELECT request_hash, response_status, response_resource "
        "FROM interface_idempotency_keys "
        "WHERE actor_scope=? AND operation=? AND idem_key=?",
        (actor.scope, operation, key)).fetchone()
    if row is not None:
        if expired:
            con.commit()
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


def _idempotent_atomic(con, actor: _Actor, operation: str, headers, body_obj,
                       produce, response_headers=None):
    """Idempotency plus one transaction for lifecycle resource creation.

    Unlike the older board helper, ``produce`` never commits. A failed
    declaration is rolled back to its savepoint before its error receipt is
    stored, so no orphan document/review/sprint row can survive.
    """
    key = headers.get("Idempotency-Key") or ""
    if not key:
        return _err(
            422,
            "idempotency_key_required",
            "Idempotency-Key header is required for this mutation",
        )
    canonical = hashlib.sha256(
        json.dumps(body_obj, sort_keys=True, default=str).encode()
    ).hexdigest()
    con.execute(
        "DELETE FROM interface_idempotency_keys "
        "WHERE expires_at <= datetime('now')"
    )
    row = con.execute(
        "SELECT request_hash,response_status,response_resource "
        "FROM interface_idempotency_keys "
        "WHERE actor_scope=? AND operation=? AND idem_key=?",
        (actor.scope, operation, key),
    ).fetchone()
    if row is not None:
        if row[0] != canonical:
            con.commit()
            return _err(
                409,
                "idempotency_conflict",
                "Idempotency-Key reused with a different request body",
            )
        obj = json.loads(row[2])
        con.commit()
        extra = response_headers(obj) if response_headers else None
        return _json(row[1], obj, extra)

    con.execute("SAVEPOINT sprint_resource")
    savepoint_open = True
    try:
        status, obj = produce()
        if status >= 400:
            con.execute("ROLLBACK TO sprint_resource")
        con.execute("RELEASE sprint_resource")
        savepoint_open = False
        con.execute(
            "INSERT INTO interface_idempotency_keys "
            "(actor_scope,operation,idem_key,request_hash,response_status,"
            " response_resource,expires_at) "
            "VALUES (?,?,?,?,?,?,datetime('now',?))",
            (
                actor.scope,
                operation,
                key,
                canonical,
                status,
                json.dumps(obj, default=str),
                f"+{IDEM_TTL_S} seconds",
            ),
        )
        con.commit()
    except Exception:
        if savepoint_open:
            con.execute("ROLLBACK TO sprint_resource")
            con.execute("RELEASE sprint_resource")
        con.rollback()
        raise
    extra = response_headers(obj) if response_headers else None
    return _json(status, obj, extra)


def _idempotent_atomic_replay(
    con,
    actor: _Actor,
    operation: str,
    headers,
    body_obj,
    response_headers=None,
):
    """Return a still-live atomic mutation replay before external preparation.

    Fresh arming must inspect Conductor config and route files, but an exact
    retry must not become dependent on config that changed after the commit.
    The full helper remains the transactional authority for fresh requests.
    """
    key = headers.get("Idempotency-Key") or ""
    if not key:
        return None
    canonical = hashlib.sha256(
        json.dumps(body_obj, sort_keys=True, default=str).encode()
    ).hexdigest()
    row = con.execute(
        "SELECT request_hash,response_status,response_resource "
        "FROM interface_idempotency_keys "
        "WHERE actor_scope=? AND operation=? AND idem_key=? "
        "AND expires_at>datetime('now')",
        (actor.scope, operation, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != canonical:
        return _err(
            409,
            "idempotency_conflict",
            "Idempotency-Key reused with a different request body",
        )
    obj = json.loads(row["response_resource"])
    extra = response_headers(obj) if response_headers else None
    return _json(row["response_status"], obj, extra)


# ------------------------------------------------------- the board as a record

# The board's columns as the planner edits them, minus `state` — which is
# deliberately NOT here (see _patch_sprint_unit). Keyed by request field name
# (identical to the column name), valued by the type the boundary demands and
# whether an explicit null may clear it.
#
# ONE definition, because POST and PATCH both admit these fields and a second
# statement of their shape would drift: the boundary that existed in `add` and
# not in `set` is precisely how a numeric title 500'd on one route and a
# textual pr_number landed in an INTEGER column on the other.
_UNIT_FIELDS = {
    "unit_title": (str, False),
    "depends_on": (str, True),
    "overlap": (str, True),
    "branch": (str, True),
    "pr_number": (int, True),
    # The reviewer's `review-clean head=<sha>` verdict, as the planner records
    # it when moving a unit out of in_review (H-14). Free text on purpose:
    # close checks PRESENCE, never correctness — judging whether that SHA is
    # the head that was reviewed is the planner's call (decision #76), and a
    # format check here would only teach a planner to type past it.
    "review_head": (str, True),
}
_UNIT_ROLES = {"dev": "dev_shell_id", "reviewer": "reviewer_shell_id"}
# The two role columns, in one order, derived from the single definition
# above — the counterpart lookup below indexes into this.
_ROLE_COLS = tuple(_UNIT_ROLES.values())


def _is_int(value) -> bool:
    """`True` is an `int` in Python, so a plain isinstance check would accept
    `{"sprint_doc_id": true}` and address the board of document 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _qint(query: dict, name: str) -> "int | None":
    raw = query.get(name, [None])[0]
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bad_unit_field(body):
    """Type-check every writable board field AT THE EDGE, before any of it can
    reach a string method or an INTEGER column.

    The board is a record of what a planner declared, and the reconciler acts
    on what it is handed — so a field that lands holding something no planner
    could have typed (a PR number that is text, a branch that is a number) is
    belief corrupted quietly, which is worse than the 500 that the same input
    produced on the other route. Returns an error response, or None to allow.
    """
    for field, (kind, clearable) in _UNIT_FIELDS.items():
        if field not in body:
            continue                      # omitted = leave alone, not clear
        value = body[field]
        if value is None:
            if clearable:
                continue
            return _err(422, "validation", f"{field} cannot be cleared")
        if kind is int and not _is_int(value):
            return _err(422, "validation", f"{field} must be an integer or null")
        if kind is str and not isinstance(value, str):
            return _err(422, "validation",
                        f"{field} must be a string"
                        + (" or null" if clearable else ""))
        if kind is str and not value.strip():
            # A blank is not a value: an empty `branch` reads as a declared
            # branch that is the empty string, which U3/U4 compare against the
            # worktree. Retract it with an explicit null instead.
            return _err(422, "validation",
                        f"{field} cannot be blank"
                        + (" — pass null to clear it" if clearable else ""))
    return None


def _no_such_move(doc_id, seq: str, was: str, now: str) -> str:
    """The refusal a planner reads when it asks the board for a move the
    machine does not have (H-11). Terminal gets its own sentence because the
    remedy is different in kind: every other illegal move is a typo to retype,
    while a wrong `merged`/`cancelled` is a declaration that has to be
    superseded rather than undone."""
    if was in _TERMINAL_UNIT_STATES:
        return (f"sprint {doc_id} unit {seq} is {was} — terminal, and terminal "
                f"has no exits. The row is the record of what was declared, so "
                f"a mis-declared {was} is corrected by declaring a SUCCESSOR "
                f"UNIT at a new seq that redoes or disposes of the work, not "
                f"by re-opening this one")
    legal = sorted(SPRINT_UNIT_EDGES.get(was, ()))
    return (f"sprint {doc_id} unit {seq} cannot go {was} -> {now}; from {was} "
            f"the board admits {', '.join(legal)}")


def _pr_claimed_by(con, doc_id, pr_number, seq: str):
    """The partial unique index idx_sprint_units_pr_claim (0109) fires as a
    bare IntegrityError, which reads to a planner as "the write failed" and
    names neither the PR nor the unit already holding it. Translate it: return
    an error object when some OTHER unit on this board already claims
    `pr_number`, else None so the caller reports its own conflict.

    Re-derived by query rather than parsed out of the driver's message —
    every driver phrases a constraint violation differently, and the answer a
    planner needs (WHICH unit) is not in that string on any of them.
    """
    if pr_number is None:
        return None
    row = con.execute(
        "SELECT seq FROM sprint_units "
        "WHERE sprint_doc_id=? AND pr_number=? AND seq<>?",
        (doc_id, pr_number, seq)).fetchone()
    if row is None:
        return None
    return _err_obj(
        "pr_already_claimed",
        f"PR #{pr_number} is already unit {row[0]}'s on sprint {doc_id} — one "
        f"PR belongs to one unit, and pointing {seq} at it too would leave "
        "the reconciler resolving its events to whichever row it read first")


def _may_write_board(con, actor, sprint_doc_id: int):
    """Devs and reviewers never write the board (FnB directive). This is not a
    permission detail — a worker that could mark its own unit done would make
    the board agree with reality BY CONSTRUCTION, and the reconciler's whole
    value is the disagreement. Returns an error tuple, or None to allow."""
    if actor.kind != "shell":
        return None                       # the operator owns everything
    owner = con.execute(
        "SELECT planner_shell_id,state FROM sprints WHERE sprint_doc_id=?",
        (sprint_doc_id,),
    ).fetchone()
    if owner is None:
        return _err(
            422,
            "undeclared_sprint",
            f"document {sprint_doc_id} has no authoritative sprint record",
        )
    if owner["state"] == "needs_owner":
        return _err(
            409,
            "sprint_needs_owner",
            f"legacy sprint {sprint_doc_id} must be adopted before board writes",
        )
    if con.execute(
        "SELECT 1 FROM sprint_cancellations WHERE sprint_doc_id=?",
        (sprint_doc_id,),
    ).fetchone() is not None:
        return _err(
            409,
            "sprint_cancelled",
            f"sprint {sprint_doc_id} has an operator cancellation request; "
            "its board is read-only while the Planner writes the abort report",
        )
    if owner["planner_shell_id"] == actor.shell_id:
        return None
    return _err(
        403,
        "not_sprint_owner",
        f"shell {actor.shell_id} is not sprint {sprint_doc_id}'s originating "
        "Planner",
    )


def _resolve_shell(con, value):
    """A role slot from a request: a shell_id, a shortname, or None to clear.
    Raises _BadShell so a typo'd shortname is a 422 and never a silently
    unassigned role — an empty role column is a state the reconciler reads as
    "nobody is expected here"."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _BadShell(f"not a shell reference: {value!r}")
    if isinstance(value, int):
        row = con.execute(
            "SELECT shell_id FROM shells WHERE shell_id=? "
            "AND COALESCE(is_deleted,0)=0", (value,)).fetchone()
        if row is None:
            raise _BadShell(f"no such shell: {value}")
        return row[0]
    if isinstance(value, str) and value.strip():
        row = con.execute(
            "SELECT shell_id FROM shells WHERE shortname=? COLLATE NOCASE "
            "AND COALESCE(is_deleted,0)=0", (value.strip(),)).fetchone()
        if row is None:
            raise _BadShell(f"no such shell: {value.strip()!r}")
        return row[0]
    raise _BadShell(f"not a shell reference: {value!r}")


def _resolve_role_shell(con, value, role: str):
    shell_id = _resolve_shell(con, value)
    if shell_id is None:
        return None
    row = con.execute(
        "SELECT flavor FROM shells WHERE shell_id=?",
        (shell_id,),
    ).fetchone()
    if row is None or row["flavor"] != role:
        raise _BadShell(f"shell {value!r} is not an active {role} shell")
    return shell_id


class _BadShell(Exception):
    pass


_UNIT_COLS = (
    "unit_id", "sprint_doc_id", "seq", "unit_title", "dev_shell_id",
    "reviewer_shell_id", "state", "depends_on", "overlap", "branch",
    "pr_number", "review_head", "assigned_at", "state_changed_at",
    "updated_at", "updated_by_shell_id")


def _unit_projection(con, unit_id: int) -> dict:
    row = con.execute(
        f"SELECT {', '.join(_UNIT_COLS)} FROM sprint_units WHERE unit_id=?",
        (unit_id,)).fetchone()
    unit = dict(zip(_UNIT_COLS, row))
    for role, col in _UNIT_ROLES.items():
        name = None
        if unit[col] is not None:
            r = con.execute("SELECT shortname FROM shells WHERE shell_id=?",
                            (unit[col],)).fetchone()
            name = r[0] if r else None
        unit[f"{role}_shortname"] = name
    return unit


# ------------------------------------------------- assignment change notice

def _counterpart(col: str) -> str:
    """The other role on the unit. A unit has exactly two roles — the pair IS
    the unit — so "the counterpart" is total and needs no default."""
    return _ROLE_COLS[1 - _ROLE_COLS.index(col)]


def _assignment_parties(before: dict, after: dict) -> set:
    """The shells one board write must tell — the spec's rule and nothing
    wider (doc 58 "Assignment change notice"): for each role that ACTUALLY
    moved, the shell newly named, the shell it replaced, and the counterpart
    role on that unit as the board now reads.

    The set is read OFF THE RECORD. There is no subscription and no reader
    log because the audit that retired feature #29 found the shells who need
    telling are exactly the shells the record names — so any rule broader
    than "these columns" is the defect this emitter exists to avoid, not a
    generalisation of it.

    A role re-asserted to the same shell has not moved and tells nobody: the
    sprint-52 incident was a column that CHANGED under two workers, and a
    planner re-typing the value it already holds is not that.
    """
    parties = set()
    for col in _ROLE_COLS:
        if before.get(col) == after.get(col):
            continue
        parties.update({before.get(col), after.get(col),
                        after.get(_counterpart(col))})
    parties.discard(None)
    return parties


def _role_phrase(con, before: dict, after: dict) -> str:
    named = []
    for role, col in _UNIT_ROLES.items():
        if before.get(col) != after.get(col):
            named.append(f"{role} {_shortname(con, before.get(col))} -> "
                         f"{_shortname(con, after.get(col))}")
    roster = ", ".join(f"{role} {_shortname(con, after.get(col))}"
                       for role, col in _UNIT_ROLES.items())
    return f"{', '.join(named)}. Now: {roster}."


def _shortname(con, shell_id) -> str:
    if shell_id is None:
        return "unassigned"
    row = con.execute("SELECT shortname FROM shells WHERE shell_id=?",
                      (shell_id,)).fetchone()
    return row[0] if row is not None else f"shell {shell_id}"


def _emit_assignment_notice(con, actor, doc_id: int, seq: str,
                            before: dict, after: dict) -> int:
    """The remainder of retired feature #29 (spec doc 58, decision #74): one
    row per shell whose relationship to the unit changed, only on change, only
    while the sprint is LIVE. Returns the number of rows written.

    THE INVARIANT IS "AT MOST ONE ROW PER SHELL PER WRITE", not a row count.
    The spec's "at most three" was a proxy for it and is wrong at the edge: a
    PATCH that moves BOTH roles at once tells four shells — two vacated, two
    arriving — and each of them exactly once. Stating the ceiling instead of
    the invariant is what put a false number in three places at once.

    Called from inside the board write's own transaction, before its commit,
    so a board write that rolls back tells nobody it happened — and the
    routes' Idempotency-Key discipline means a replayed request returns its
    cached response without reaching this at all. That is why there is no
    dedupe_key here: the write it rides is already idempotent.

    ONE BODY, delivered to each party rather than three phrasings of one
    fact. Every recipient can locate itself in it, and a per-role restatement
    is the drift shape this spec keeps closing.

    `kind='shell'` DELIBERATELY: task / result / pr_event are wake-eligible
    kinds, and a notice is not work — `shell` is inert by construction. The
    sprint scope is still stamped so the rows filter with the rest of the
    sprint's traffic.
    """
    if not sprint_state.is_live_sprint(con, doc_id):
        return 0
    parties = _assignment_parties(before, after)
    if actor.kind == "shell":
        # The shell that just wrote the board does not need telling what it
        # wrote. This can only take the count BELOW the spec's ceiling.
        parties.discard(actor.shell_id)
    live = [s for s in sorted(parties) if con.execute(
        "SELECT 1 FROM shells WHERE shell_id=? AND COALESCE(is_deleted,0)=0",
        (s,)).fetchone() is not None]
    if not live:
        return 0
    body = (f"sprint {doc_id} unit {seq} assignment change: "
            + _role_phrase(con, before, after))
    # from_shell_id is NOT NULL and the operator carries no shell, so an
    # operator-authored notice self-addresses at each recipient. A shell write
    # names its actual actor; no retired binding is consulted and no synthetic
    # planner is minted.
    sender = actor.shell_id if actor.kind == "shell" else None
    for shell_id in live:
        con.execute(
            "INSERT INTO shell_messages "
            "(from_shell_id, to_shell_id, body, kind, sprint_doc_id) "
            "VALUES (?,?,?,'shell',?)",
            (sender if sender is not None else shell_id, shell_id, body,
             doc_id))
    _log(f"sprint board: doc={doc_id} unit={seq} assignment change "
         f"notified {len(live)} shell(s)")
    return len(live)


# ------------------------------------------------------------------ handlers

def _sprint_units(actor, query: dict):
    """GET /api/sprint-units?sprint_doc_id=N — the board, read. Every
    participant reads it (it is the board they work from); only the planner
    writes it."""
    doc_id = _qint(query, "sprint_doc_id")
    if query.get("sprint_doc_id", [None])[0] not in (None, "") \
            and doc_id is None:
        # _qint answers None for both "absent" and "unparseable", and the two
        # mean opposite things here: absent asks for every board, unparseable
        # would SILENTLY WIDEN to every board while the caller believes it
        # asked about one sprint. A filter that fails open is worse than no
        # filter, because the caller acts on units it never asked about.
        return _err(422, "validation",
                    "sprint_doc_id filter must be an integer")
    sql = "SELECT unit_id FROM sprint_units"
    params = []
    if doc_id is not None:
        sql += " WHERE sprint_doc_id=?"
        params.append(doc_id)
    # LENGTH BEFORE VALUE, because `seq` is TEXT and a plain sort is
    # lexicographic: U1, U2, U3, U10, U11 reads back as U1, U10, U11, U2, U3.
    # The board is a WORK ORDER — a planner reads it top to bottom and a
    # reader that scrambles at U10 misreports what runs next. Sorting by
    # length first makes same-prefix numbering natural (every U0-U9 before
    # every U10-U99) without inventing a numeric column the planner would
    # then have to keep in sync. Seqs of unlike shape (U-H beside U9) still
    # order by length then value — deterministic, and no worse than today.
    sql += " ORDER BY sprint_doc_id, LENGTH(seq), seq"
    con = _db()
    try:
        units = [_unit_projection(con, r[0])
                 for r in con.execute(sql, params).fetchall()]
        return _json(200, {"units": units})
    finally:
        con.close()


def _now(con) -> str:
    return con.execute("SELECT datetime('now')").fetchone()[0]


# ------------------------------------------------ reviewed spec + declaration

_QAQC_FIELDS = frozenset(("spec_doc_id", "verdict", "findings_doc_id"))
_DECLARE_FIELDS = frozenset(
    ("spec_doc_id", "title", "planner_route", "dev_route", "reviewer_route")
)
_ADOPT_FIELDS = frozenset(
    (
        "planner",
        "spec_doc_id",
        "planner_route",
        "dev_route",
        "reviewer_route",
        "evidence",
    )
)
_ARM_FIELDS = frozenset(("state",))
_CANCEL_FIELDS = frozenset(("reason",))
_ABORT_FIELDS = frozenset(("state", "report"))


def _unknown_fields(body: dict, allowed: frozenset, resource: str):
    unknown = sorted(set(body) - allowed)
    if not unknown:
        return None
    return _err(
        422,
        "validation",
        f"unknown {resource} field(s): {', '.join(unknown)}",
    )


def _actor_flavor(con, actor: _Actor):
    if actor.kind != "shell":
        return None
    return con.execute(
        "SELECT flavor FROM shells WHERE shell_id=? "
        "AND COALESCE(is_deleted,0)=0",
        (actor.shell_id,),
    ).fetchone()


def _review_projection(con, review_id: int) -> dict:
    row = con.execute(
        "SELECT q.review_id,q.spec_doc_id,q.reviewer_shell_id,"
        "q.body_sha256,q.verdict,q.findings_doc_id,q.completed_at,"
        "s.shortname AS reviewer_shortname "
        "FROM spec_qaqc_reviews q "
        "JOIN shells s ON s.shell_id=q.reviewer_shell_id "
        "WHERE q.review_id=?",
        (review_id,),
    ).fetchone()
    return dict(row)


def _create_qaqc(actor: _Actor, headers, body: dict):
    bad = _unknown_fields(body, _QAQC_FIELDS, "QAQC")
    if bad is not None:
        return bad
    spec_doc_id = body.get("spec_doc_id")
    verdict = body.get("verdict")
    findings_doc_id = body.get("findings_doc_id")
    if not _is_int(spec_doc_id):
        return _err(422, "validation", "spec_doc_id must be an integer")
    if verdict not in ("approved", "changes_requested"):
        return _err(
            422,
            "validation",
            "verdict must be approved or changes_requested",
        )
    if findings_doc_id is not None and not _is_int(findings_doc_id):
        return _err(422, "validation", "findings_doc_id must be an integer")
    con = _db()
    try:
        flavor = _actor_flavor(con, actor)
        if flavor is None or flavor[0] != "reviewer":
            return _err(
                403,
                "reviewer_required",
                "QAQC completion requires an active reviewer shell token",
            )

        def produce():
            spec = con.execute(
                "SELECT kind,body FROM documents WHERE document_id=?",
                (spec_doc_id,),
            ).fetchone()
            if spec is None or spec["kind"] != "spec":
                return 422, _err_obj(
                    "not_a_spec",
                    f"document {spec_doc_id} is not a spec",
                )
            if findings_doc_id is not None:
                findings = con.execute(
                    "SELECT kind FROM documents WHERE document_id=?",
                    (findings_doc_id,),
                ).fetchone()
                if findings is None or findings["kind"] != "doc":
                    return 422, _err_obj(
                        "invalid_findings_doc",
                        "findings_doc_id must name a doc document",
                    )
            cur = con.execute(
                "INSERT INTO spec_qaqc_reviews "
                "(spec_doc_id,reviewer_shell_id,body_sha256,verdict,"
                " findings_doc_id) VALUES (?,?,?,?,?)",
                (
                    spec_doc_id,
                    actor.shell_id,
                    sprint_lifecycle.body_sha256(spec["body"]),
                    verdict,
                    findings_doc_id,
                ),
            )
            return 201, _review_projection(con, cur.lastrowid)

        return _idempotent_atomic(
            con, actor, "spec_qaqc_review_create", headers, body, produce
        )
    finally:
        con.close()


def _list_qaqc(query: dict):
    raw = query.get("spec_doc_id", [None])[0]
    try:
        spec_doc_id = int(raw)
    except (TypeError, ValueError):
        return _err(
            422,
            "validation",
            "spec_doc_id query parameter is required and must be an integer",
        )
    con = _db()
    try:
        ids = con.execute(
            "SELECT review_id FROM spec_qaqc_reviews "
            "WHERE spec_doc_id=? ORDER BY review_id",
            (spec_doc_id,),
        ).fetchall()
        return _json(
            200,
            {"reviews": [_review_projection(con, row[0]) for row in ids]},
        )
    finally:
        con.close()


def _validate_route(con, route, field: str):
    try:
        harness, selector = sprint_lifecycle.split_route(route)
    except sprint_lifecycle.SprintLifecycleError as exc:
        return None, _err_obj("invalid_route", f"{field}: {exc}")
    row = con.execute(
        "SELECT availability,headless_supported FROM model_routes "
        "WHERE harness=? AND selector=?",
        (harness, selector),
    ).fetchone()
    if row is None or row["availability"] != "available" \
            or not row["headless_supported"]:
        return None, _err_obj(
            "route_not_runnable",
            f"{field} route {harness}/{selector} is not locally headless-runnable",
        )
    return f"{harness}/{selector}", None


def _append_conversation_event(
    con,
    conversation_id: str,
    event_type: str,
    payload: dict,
    *,
    message_id: int | None = None,
    run_id: int | None = None,
) -> int:
    return sprint_conversations.append_event(
        con,
        conversation_id,
        event_type,
        payload,
        message_id=message_id,
        run_id=run_id,
    )


def _conversation_route(con, shell, harness: str, model: str | None):
    adapter = run_mod.load_adapter(harness)
    effort = run_mod.default_headless_effort(adapter)
    try:
        run_mod.validate_headless_request(adapter, model, effort)
    except ValueError as exc:
        return None, _err_obj("route_not_runnable", str(exc))
    worktree = run_mod.shell_work_dir(
        shell["shortname"], shell["flavor"]
    ).resolve(strict=False)
    if worktree.exists() and not worktree.is_dir():
        return None, _err_obj(
            "worktree_invalid",
            f"shell {shell['shortname']!r} worktree is not a directory",
        )
    return {
        "harness": harness,
        "provider": run_mod.session_provider(harness, model),
        "model": model,
        "effort": effort,
        "worktree": str(worktree),
    }, None


def _create_sprint_conversation(
    con,
    *,
    sprint_doc_id: int,
    shell,
    role: str,
    lifecycle: str,
    route: dict,
    title: str,
    creation_key: str,
    prompt: str,
    unit_id: int | None = None,
    required_result_kind: str | None = None,
    source_directive_id: int | None = None,
) -> str:
    return sprint_conversations.create_sprint_conversation(
        con,
        sprint_doc_id=sprint_doc_id,
        shell=shell,
        role=role,
        lifecycle=lifecycle,
        route=route,
        title=title,
        creation_key=creation_key,
        prompt=prompt,
        unit_id=unit_id,
        required_result_kind=required_result_kind,
        source_directive_id=source_directive_id,
    )


def _conductor_projection(con, sprint_doc_id: int):
    row = con.execute(
        "SELECT c.conversation_id,c.state,c.created_at,c.last_activity_at,"
        "s.shell_id,s.shortname,s.display_name "
        "FROM sprint_conversation_bindings b "
        "JOIN conversations c ON c.conversation_id=b.conversation_id "
        "JOIN shells s ON s.shell_id=c.shell_id "
        "WHERE b.sprint_doc_id=? AND b.role='conductor' "
        "ORDER BY b.binding_id DESC LIMIT 1",
        (sprint_doc_id,),
    ).fetchone()
    if row is None:
        return None
    messages = [
        {
            "message_id": int(item["message_id"]),
            "sender_kind": item["sender_kind"],
            "message_kind": item["message_kind"],
            "body": item["body"],
            "state": item["state"],
            "created_at": item["created_at"],
        }
        for item in con.execute(
            "SELECT message_id,sender_kind,message_kind,body,state,created_at "
            "FROM conversation_messages WHERE conversation_id=? "
            "AND message_kind<>'control' ORDER BY message_id DESC LIMIT 50",
            (row["conversation_id"],),
        ).fetchall()[::-1]
    ]
    assistant = [
        {
            "sequence": int(item["sequence"]),
            "text": json.loads(item["payload"]).get("text", ""),
            "created_at": item["created_at"],
        }
        for item in con.execute(
            "SELECT sequence,payload,created_at FROM conversation_events "
            "WHERE conversation_id=? AND event_type='assistant.delta' "
            "ORDER BY sequence DESC LIMIT 200",
            (row["conversation_id"],),
        ).fetchall()[::-1]
    ]
    events = []
    for item in con.execute(
        "SELECT sequence,event_type,payload,message_id,run_id,created_at "
        "FROM conversation_events WHERE conversation_id=? "
        "ORDER BY sequence DESC LIMIT 200",
        (row["conversation_id"],),
    ).fetchall()[::-1]:
        try:
            payload = json.loads(item["payload"])
        except (TypeError, ValueError):
            payload = {}
        events.append(
            {
                "sequence": int(item["sequence"]),
                "event_type": item["event_type"],
                "payload": payload,
                "message_id": item["message_id"],
                "run_id": item["run_id"],
                "created_at": item["created_at"],
            }
        )
    latest_run = con.execute(
        "SELECT run_id,state,error_code,error_detail,started_at,ended_at "
        "FROM conversation_runs WHERE conversation_id=? "
        "ORDER BY run_id DESC LIMIT 1",
        (row["conversation_id"],),
    ).fetchone()
    return {
        "conversation_id": row["conversation_id"],
        "state": row["state"],
        "created_at": row["created_at"],
        "last_activity_at": row["last_activity_at"],
        "shell": {
            "shell_id": int(row["shell_id"]),
            "shortname": row["shortname"],
            "display_name": row["display_name"],
        },
        "messages": messages,
        "assistant": assistant,
        "events": events,
        "run": dict(latest_run) if latest_run is not None else None,
    }


def _cancellation_projection(con, sprint_doc_id: int):
    row = con.execute(
        "SELECT cancellation_id,sprint_doc_id,reason,state,"
        "planner_conversation_id,requested_at,abort_report,"
        "completed_by_shell_id,completed_at "
        "FROM sprint_cancellations WHERE sprint_doc_id=?",
        (sprint_doc_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _assignment_projections(con, sprint_doc_id: int):
    rows = con.execute(
        "SELECT b.binding_id,b.conversation_id,b.role,b.lifecycle,b.slot,"
        "b.unit_id,u.seq AS unit_seq,b.source_directive_id,"
        "b.required_result_kind,b.state,b.outcome,b.result_message_id,"
        "b.created_at,b.started_at,b.completed_at,"
        "c.state AS conversation_state,s.shell_id,s.shortname,s.display_name,"
        "ar.result_kind,ar.directive_id,m.body AS result_body,"
        "r.run_id,r.state AS run_state,r.error_code,r.error_detail,"
        "r.started_at AS run_started_at,r.ended_at AS run_ended_at "
        "FROM sprint_conversation_bindings b "
        "JOIN conversations c ON c.conversation_id=b.conversation_id "
        "JOIN shells s ON s.shell_id=c.shell_id "
        "LEFT JOIN sprint_units u ON u.unit_id=b.unit_id "
        "LEFT JOIN sprint_assignment_results ar ON ar.binding_id=b.binding_id "
        "LEFT JOIN shell_messages m ON m.message_id=ar.message_id "
        "LEFT JOIN conversation_runs r ON r.run_id=("
        " SELECT latest.run_id FROM conversation_runs latest "
        " WHERE latest.conversation_id=b.conversation_id "
        " ORDER BY latest.run_id DESC LIMIT 1"
        ") "
        "WHERE b.sprint_doc_id=? AND b.role<>'conductor' "
        "ORDER BY b.binding_id",
        (sprint_doc_id,),
    ).fetchall()
    assignments = []
    for row in rows:
        failure = con.execute(
            "SELECT evidence,observed_at FROM sentinel_events "
            "WHERE sprint_doc_id=? AND event_kind='worker-failed' "
            "AND json_extract(evidence,'$.binding_id')=? "
            "ORDER BY event_id DESC LIMIT 1",
            (sprint_doc_id, row["binding_id"]),
        ).fetchone()
        failure_evidence = None
        if failure is not None:
            try:
                parsed_evidence = json.loads(failure["evidence"])
            except (TypeError, ValueError):
                parsed_evidence = {}
            failure_evidence = (
                parsed_evidence
                if isinstance(parsed_evidence, dict)
                else {"evidence": parsed_evidence}
            )
            failure_evidence["observed_at"] = failure["observed_at"]
        if row["outcome"] is not None:
            display_state = {
                "succeeded": "closed",
                "failed": "failed",
                "unknown": "unknown",
                "cancelled": "cancelled",
                "closed": "closed",
            }.get(row["outcome"], row["outcome"])
        elif row["run_state"] in ("leased", "starting", "running"):
            display_state = "working"
        elif row["conversation_state"] in ("queued", "running"):
            display_state = (
                "working"
                if row["conversation_state"] == "running"
                else "queued"
            )
        elif row["conversation_state"] in ("waiting", "error"):
            display_state = (
                "waiting"
                if row["conversation_state"] == "waiting"
                else "failed"
            )
        else:
            display_state = "idle"
        assignments.append(
            {
                "binding_id": int(row["binding_id"]),
                "conversation_id": row["conversation_id"],
                "role": row["role"],
                "lifecycle": row["lifecycle"],
                "slot": row["slot"],
                "unit_id": row["unit_id"],
                "unit_seq": row["unit_seq"],
                "source_directive_id": row["source_directive_id"],
                "required_result_kind": row["required_result_kind"],
                "state": row["state"],
                "outcome": row["outcome"],
                "display_state": display_state,
                "result_message_id": row["result_message_id"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "conversation_state": row["conversation_state"],
                "shell": {
                    "shell_id": int(row["shell_id"]),
                    "shortname": row["shortname"],
                    "display_name": row["display_name"],
                },
                "run": (
                    None
                    if row["run_id"] is None
                    else {
                        "run_id": int(row["run_id"]),
                        "state": row["run_state"],
                        "error_code": row["error_code"],
                        "error_detail": row["error_detail"],
                        "started_at": row["run_started_at"],
                        "ended_at": row["run_ended_at"],
                    }
                ),
                "result": (
                    None
                    if row["result_message_id"] is None
                    else {
                        "message_id": int(row["result_message_id"]),
                        "result_kind": row["result_kind"],
                        "directive_id": row["directive_id"],
                        "body": row["result_body"],
                    }
                ),
                "error_evidence": failure_evidence,
            }
        )
    return assignments


def _sprint_projection(con, sprint_doc_id: int):
    row = con.execute(
        "SELECT sp.*,d.title AS sprint_title,d.feature_id,d.frozen,"
        "d.created_at,feature.title AS feature_title,"
        "CASE WHEN sp.handed_off_at LIKE '____-__-__%' "
        "THEN strftime('%Y-%m-%dT%H:%M:%SZ',sp.handed_off_at) "
        "ELSE NULL END AS projected_started_at,"
        "spec.title AS spec_title,planner.shortname AS planner_shortname,"
        "q.body_sha256 AS qaqc_body_sha256,q.verdict AS qaqc_verdict,"
        "q.completed_at AS qaqc_completed_at "
        "FROM sprints sp "
        "JOIN documents d ON d.document_id=sp.sprint_doc_id "
        "LEFT JOIN roadmap feature ON feature.feature_id=d.feature_id "
        "LEFT JOIN documents spec ON spec.document_id=sp.spec_doc_id "
        "LEFT JOIN shells planner ON planner.shell_id=sp.planner_shell_id "
        "LEFT JOIN spec_qaqc_reviews q ON q.review_id=sp.qaqc_review_id "
        "WHERE sp.sprint_doc_id=?",
        (sprint_doc_id,),
    ).fetchone()
    if row is None:
        return None
    title = row["sprint_title"]
    if title is not None and title.upper().startswith("SPRINT:") \
            and not title[len("SPRINT:"):].strip():
        title = f"Sprint #{row['sprint_doc_id']}"
    cancellation = _cancellation_projection(con, sprint_doc_id)
    display_state = (
        "cancelling"
        if cancellation is not None
        and cancellation["state"] == "requested"
        and row["state"] not in ("closed", "aborted")
        else row["state"]
    )
    out = {
        "document_id": row["sprint_doc_id"],
        "sprint_doc_id": row["sprint_doc_id"],
        "title": title,
        "state": row["state"],
        "display_state": display_state,
        "legacy": bool(row["legacy"]),
        "declared_at": row["declared_at"],
        "handed_off_at": row["handed_off_at"],
        "started_at": row["projected_started_at"],
        "closed_at": row["closed_at"],
        "planner_route": row["planner_route"],
        "dev_route": row["dev_route"],
        "reviewer_route": row["reviewer_route"],
        "planner": (
            None
            if row["planner_shell_id"] is None
            else {
                "shell_id": row["planner_shell_id"],
                "shortname": row["planner_shortname"],
            }
        ),
        "feature": (
            None
            if row["feature_id"] is None
            else {
                "feature_id": row["feature_id"],
                "title": row["feature_title"],
            }
        ),
        "spec": (
            None
            if row["spec_doc_id"] is None
            else {
                "document_id": row["spec_doc_id"],
                "title": row["spec_title"],
            }
        ),
        "qaqc": (
            None
            if row["qaqc_review_id"] is None
            else {
                "review_id": row["qaqc_review_id"],
                "body_sha256": row["qaqc_body_sha256"],
                "verdict": row["qaqc_verdict"],
                "completed_at": row["qaqc_completed_at"],
            }
        ),
        "conductor": _conductor_projection(con, sprint_doc_id),
        "cancellation": cancellation,
        "assignments": _assignment_projections(con, sprint_doc_id),
        "units": [],
    }
    unit_ids = con.execute(
        "SELECT unit_id FROM sprint_units WHERE sprint_doc_id=? "
        "ORDER BY LENGTH(seq),seq",
        (sprint_doc_id,),
    ).fetchall()
    for unit_id in unit_ids:
        unit = _unit_projection(con, unit_id[0])
        unit["state_recognized"] = unit["state"] in _UNIT_STATES
        out["units"].append(unit)
    return out


def _sprint_overview(con, recent: int = 5) -> dict:
    live_ids = [
        int(row[0])
        for row in con.execute(
            "SELECT sprint_doc_id FROM sprints "
            "WHERE state IN ('declared','active','closing') "
            "ORDER BY sprint_doc_id"
        ).fetchall()
    ]
    recent_ids = [
        int(row[0])
        for row in con.execute(
            "SELECT sprint_doc_id FROM sprints "
            "WHERE state IN ('closed','aborted') "
            "ORDER BY COALESCE(closed_at,declared_at) DESC,sprint_doc_id DESC "
            "LIMIT ?",
            (recent,),
        ).fetchall()
    ]
    sprints = [
        _sprint_projection(con, sprint_doc_id)
        for sprint_doc_id in (*live_ids, *recent_ids)
    ]
    return {
        "active_count": sum(
            sprint["state"] == "active"
            and sprint["display_state"] == "active"
            for sprint in sprints
        ),
        "open_count": sum(
            sprint["state"] in ("declared", "active", "closing")
            for sprint in sprints
        ),
        "sprints": sprints,
    }


def _create_sprint(actor: _Actor, headers, body: dict):
    bad = _unknown_fields(body, _DECLARE_FIELDS, "sprint")
    if bad is not None:
        return bad
    spec_doc_id = body.get("spec_doc_id")
    title = body.get("title")
    if not _is_int(spec_doc_id):
        return _err(422, "validation", "spec_doc_id must be an integer")
    if not isinstance(title, str) or not title.strip():
        return _err(422, "validation", "title must be a nonblank string")
    title = title.strip()
    if title.upper().startswith("SPRINT:"):
        return _err(
            422,
            "validation",
            "title must omit the SPRINT: prefix; the server owns it",
        )
    con = _db()
    try:
        flavor = _actor_flavor(con, actor)
        if flavor is None or flavor[0] != "planner":
            return _err(
                403,
                "planner_required",
                "sprint declaration requires an active Planner shell token",
            )
        def produce():
            routes = {}
            for field in ("planner_route", "dev_route", "reviewer_route"):
                route, error = _validate_route(con, body.get(field), field)
                if error is not None:
                    return 422, error
                routes[field] = route
            spec = con.execute(
                "SELECT feature_id,kind,body FROM documents WHERE document_id=?",
                (spec_doc_id,),
            ).fetchone()
            if spec is None or spec["kind"] != "spec":
                return 422, _err_obj(
                    "not_a_spec",
                    f"document {spec_doc_id} is not a spec",
                )
            current_hash = sprint_lifecycle.body_sha256(spec["body"])
            review = con.execute(
                "SELECT review_id FROM spec_qaqc_reviews "
                "WHERE spec_doc_id=? AND verdict='approved' AND body_sha256=? "
                "ORDER BY review_id DESC LIMIT 1",
                (spec_doc_id, current_hash),
            ).fetchone()
            if review is None:
                return 409, _err_obj(
                    "qaqc_required",
                    "sprint declaration requires reviewer approval for the "
                    "spec's current body",
                    {"spec_doc_id": spec_doc_id, "body_sha256": current_hash},
                )
            seq = con.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM documents "
                "WHERE kind='doc' AND feature_id IS ?",
                (spec["feature_id"],),
            ).fetchone()[0]
            sprint_title = f"SPRINT: {title}"
            prose = (
                f"# {sprint_title}\n\n"
                f"Governing spec: document #{spec_doc_id}\n\n"
                "Lifecycle state is stored in the authoritative `sprints` row.\n"
            )
            cur = con.execute(
                "INSERT INTO documents "
                "(feature_id,kind,seq,title,body) VALUES (?,'doc',?,?,?)",
                (spec["feature_id"], seq, sprint_title, prose),
            )
            sprint_doc_id = cur.lastrowid
            con.execute(
                "INSERT INTO sprints "
                "(sprint_doc_id,spec_doc_id,planner_shell_id,qaqc_review_id,"
                " planner_route,dev_route,reviewer_route,state,legacy) "
                "VALUES (?,?,?,?,?,?,?,'declared',0)",
                (
                    sprint_doc_id,
                    spec_doc_id,
                    actor.shell_id,
                    review["review_id"],
                    routes["planner_route"],
                    routes["dev_route"],
                    routes["reviewer_route"],
                ),
            )
            return 201, _sprint_projection(con, sprint_doc_id)

        return _idempotent_atomic(
            con,
            actor,
            "sprint_declare",
            headers,
            body,
            produce,
            response_headers=lambda obj: [
                ("Location", f"/api/sprints/{obj['sprint_doc_id']}")
            ] if "sprint_doc_id" in obj else [],
        )
    finally:
        con.close()


def _arm_sprint(
    actor: _Actor,
    headers,
    sprint_doc_id: int,
    body: dict,
):
    bad = _unknown_fields(body, _ARM_FIELDS, "arming")
    if bad is not None:
        return bad
    if body.get("state") != "active":
        return _err(
            422,
            "validation",
            "Planner arming requires exactly {\"state\":\"active\"}",
        )
    con = _db()
    notify_ids: list[str] = []
    try:
        flavor = _actor_flavor(con, actor)
        if flavor is None or flavor[0] != "planner":
            return _err(
                403,
                "planner_required",
                "Sprint arming requires an active Planner shell token",
            )
        replay = _idempotent_atomic_replay(
            con,
            actor,
            f"sprint_arm:{sprint_doc_id}",
            headers,
            body,
        )
        if replay is not None:
            return replay
        config = conductor_runtime.load_config()
        if not config.enabled:
            return _err(
                409,
                "conductor_disabled",
                "the persistent Conductor is disabled for this install",
            )
        conductor = con.execute(
            "SELECT shell_id,shortname,display_name,flavor FROM shells "
            "WHERE shortname=? COLLATE NOCASE AND flavor='conductor' "
            "AND COALESCE(is_deleted,0)=0",
            (config.shell,),
        ).fetchone()
        if conductor is None:
            return _err(
                409,
                "conductor_unavailable",
                f"configured Conductor shell {config.shell!r} is unavailable",
            )
        route, route_error = _conversation_route(
            con,
            conductor,
            conductor_policy.CONDUCTOR_HARNESS,
            config.model,
        )
        if route_error is not None:
            return _json(422, route_error)

        def produce():
            sprint = con.execute(
                "SELECT state,planner_shell_id FROM sprints "
                "WHERE sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone()
            if sprint is None:
                return 404, _err_obj(
                    "not_found", f"no sprint declaration {sprint_doc_id}"
                )
            if sprint["planner_shell_id"] != actor.shell_id:
                return 403, _err_obj(
                    "not_sprint_owner",
                    f"shell {actor.shell_id} is not sprint "
                    f"{sprint_doc_id}'s originating Planner",
                )
            if sprint["state"] != "declared":
                return 409, _err_obj(
                    "sprint_not_armable",
                    f"sprint {sprint_doc_id} is {sprint['state']}, not declared",
                )
            if con.execute(
                "SELECT 1 FROM sprint_cancellations WHERE sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone() is not None:
                return 409, _err_obj(
                    "sprint_cancelled",
                    f"sprint {sprint_doc_id} has been cancelled",
                )
            try:
                conductor_runtime.validate_arm_board(con, sprint_doc_id)
            except conductor_runtime.DirectiveRefused as exc:
                return 409, _err_obj("board_not_armable", str(exc))
            conversation_id = _create_sprint_conversation(
                con,
                sprint_doc_id=sprint_doc_id,
                shell=conductor,
                role="conductor",
                lifecycle="persistent",
                route=route,
                title=f"Sprint #{sprint_doc_id} Conductor",
                creation_key=f"sprint-arm:{sprint_doc_id}",
                prompt=(
                    f"Sprint #{sprint_doc_id} was armed by its originating "
                    "Planner. Oversee its mechanical workflow through "
                    "completion using the sprint_cond contract. The Planner "
                    "owns scope and the final Sprint or abort report; never "
                    "activate, cancel, or close the Sprint on the Planner's "
                    "behalf."
                ),
            )
            sprint_lifecycle.transition(con, sprint_doc_id, "active")
            directive_id = con.execute(
                "INSERT INTO directives "
                "(issuer_shell_id,issuer_flavor,kind,payload,target,"
                " sprint_doc_id,unit_id) "
                "VALUES (NULL,'system','sprint-armed','{}','conductor',?,NULL)",
                (sprint_doc_id,),
            ).lastrowid
            sprint_conversations.enqueue_conductor_directive(
                con,
                sprint_doc_id=sprint_doc_id,
                directive_id=directive_id,
                source_kind="sprint-armed",
                evidence={"armed_by_shell_id": actor.shell_id},
                idempotency_key=f"sprint-arm-release:{sprint_doc_id}",
            )
            con.execute(
                "INSERT INTO sentinel_events "
                "(event_kind,shell_id,sprint_doc_id,evidence) "
                "VALUES ('sprint-armed',?,?,?)",
                (
                    actor.shell_id,
                    sprint_doc_id,
                    json.dumps(
                        {"conductor_conversation_id": conversation_id},
                        sort_keys=True,
                    ),
                ),
            )
            notify_ids.append(conversation_id)
            return 200, _sprint_projection(con, sprint_doc_id)

        response = _idempotent_atomic(
            con,
            actor,
            f"sprint_arm:{sprint_doc_id}",
            headers,
            body,
            produce,
        )
    finally:
        con.close()
    for conversation_id in notify_ids:
        conversation_events.notify(conversation_id)
    if notify_ids:
        conversation_broker.notify_commit()
    return response


def _cancel_existing_sprint_work(con, sprint_doc_id: int):
    conversation_ids = [
        row[0]
        for row in con.execute(
            "SELECT conversation_id FROM conversations "
            "WHERE mode='sprint' AND sprint_doc_id=?",
            (sprint_doc_id,),
        )
    ]
    run_ids = [
        int(row["run_id"])
        for row in con.execute(
            "SELECT r.run_id FROM conversation_runs r "
            "JOIN conversations c ON c.conversation_id=r.conversation_id "
            "WHERE c.mode='sprint' AND c.sprint_doc_id=? "
            "AND r.state IN ('leased','starting','running')",
            (sprint_doc_id,),
        )
    ]
    for conversation_id in conversation_ids:
        queued = con.execute(
            "SELECT o.outbox_id,o.message_id,o.state "
            "FROM conversation_outbox o "
            "JOIN conversation_messages m ON m.message_id=o.message_id "
            "WHERE o.conversation_id=? AND o.state IN ('pending','claimed')",
            (conversation_id,),
        ).fetchall()
        for item in queued:
            con.execute(
                "UPDATE conversation_outbox SET state='cancelled' "
                "WHERE outbox_id=?",
                (item["outbox_id"],),
            )
            con.execute(
                "UPDATE conversation_messages SET state='cancelled',"
                "completed_at=datetime('now') WHERE message_id=? "
                "AND state IN ('accepted','queued')",
                (item["message_id"],),
            )
            _append_conversation_event(
                con,
                conversation_id,
                "message.cancelled",
                {
                    "message_id": int(item["message_id"]),
                    "reason": "Sprint cancelled by operator",
                },
                message_id=int(item["message_id"]),
            )
        con.execute(
            "UPDATE conversations SET state='idle',"
            "last_activity_at=datetime('now'),version=version+1 "
            "WHERE conversation_id=? AND state='queued' "
            "AND NOT EXISTS ("
            " SELECT 1 FROM conversation_runs r "
            " WHERE r.conversation_id=conversations.conversation_id "
            " AND r.state IN ('leased','starting','running'))",
            (conversation_id,),
        )
        binding = con.execute(
            "SELECT b.binding_id,b.lifecycle,b.state AS binding_state,"
            "c.state AS conversation_state "
            "FROM sprint_conversation_bindings b "
            "JOIN conversations c ON c.conversation_id=b.conversation_id "
            "WHERE b.conversation_id=?",
            (conversation_id,),
        ).fetchone()
        live_run = con.execute(
            "SELECT 1 FROM conversation_runs WHERE conversation_id=? "
            "AND state IN ('leased','starting','running')",
            (conversation_id,),
        ).fetchone()
        if (
            binding is not None
            and binding["lifecycle"] == "one_shot"
            and binding["binding_state"] != "terminal"
            and binding["conversation_state"] in ("idle", "waiting", "error")
            and live_run is None
        ):
            con.execute(
                "UPDATE conversations SET state='closed',"
                "closed_at=datetime('now'),last_activity_at=datetime('now'),"
                "version=version+1 WHERE conversation_id=?",
                (conversation_id,),
            )
            _append_conversation_event(
                con,
                conversation_id,
                "conversation.closed",
                {"state": "closed", "reason": "Sprint cancelled by operator"},
            )
            con.execute(
                "UPDATE sprint_conversation_bindings SET state='terminal',"
                "outcome='cancelled',completed_at=datetime('now') "
                "WHERE binding_id=?",
                (binding["binding_id"],),
            )
    for run_id in run_ids:
        row = con.execute(
            "SELECT conversation_id,trigger_message_id FROM conversation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        exists = con.execute(
            "SELECT 1 FROM conversation_events "
            "WHERE run_id=? AND event_type='run.interrupt.requested'",
            (run_id,),
        ).fetchone()
        if exists is None:
            _append_conversation_event(
                con,
                row["conversation_id"],
                "run.interrupt.requested",
                {"reason": "Sprint cancelled by operator"},
                message_id=int(row["trigger_message_id"]),
                run_id=run_id,
            )
    con.execute(
        "UPDATE directives SET status='refused',"
        "refusal_reason='Sprint cancelled by operator',"
        "executed_at=datetime('now') "
        "WHERE sprint_doc_id=? AND status='pending'",
        (sprint_doc_id,),
    )
    con.execute(
        "UPDATE sprint_units SET state='cancelled',"
        "state_changed_at=datetime('now'),updated_at=datetime('now') "
        "WHERE sprint_doc_id=? "
        "AND state NOT IN ('merged','cancelled')",
        (sprint_doc_id,),
    )
    return conversation_ids, run_ids


def _cancel_sprint(
    actor: _Actor,
    headers,
    sprint_doc_id: int,
    body: dict,
):
    if actor.kind != "operator":
        return _err(
            403,
            "operator_required",
            "Sprint cancellation is reserved to the browser operator",
        )
    bad = _unknown_fields(body, _CANCEL_FIELDS, "cancellation")
    if bad is not None:
        return bad
    reason = body.get("reason", "Cancelled by the operator")
    if not isinstance(reason, str) or not reason.strip():
        return _err(422, "validation", "reason must be a nonblank string")
    reason = reason.strip()
    if len(reason) > 2000:
        return _err(
            422, "validation", "reason must be at most 2000 characters"
        )
    normalized = {"reason": reason}
    con = _db()
    notify_ids: list[str] = []
    interrupt_ids: list[int] = []
    try:
        def location(obj):
            return [
                (
                    "Location",
                    f"/api/sprints/{sprint_doc_id}/cancellations/"
                    f"{obj['cancellation']['cancellation_id']}",
                )
            ] if "cancellation" in obj else []
        replay = _idempotent_atomic_replay(
            con,
            actor,
            f"sprint_cancel:{sprint_doc_id}",
            headers,
            normalized,
            response_headers=location,
        )
        if replay is not None:
            return replay

        def produce():
            user = con.execute(
                "SELECT user_id FROM users WHERE is_active=1 "
                "ORDER BY user_id LIMIT 1"
            ).fetchone()
            if user is None:
                return 503, _err_obj(
                    "operator_unavailable", "no active operator exists"
                )
            sprint = con.execute(
                "SELECT sp.state,sp.planner_shell_id,sp.planner_route,"
                "planner.shortname,planner.display_name,planner.flavor "
                "FROM sprints sp LEFT JOIN shells planner "
                "ON planner.shell_id=sp.planner_shell_id "
                "WHERE sp.sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone()
            if sprint is None:
                return 404, _err_obj(
                    "not_found", f"no sprint declaration {sprint_doc_id}"
                )
            if sprint["state"] not in ("declared", "active"):
                return 409, _err_obj(
                    "sprint_not_cancellable",
                    f"sprint {sprint_doc_id} is {sprint['state']}, "
                    "not declared or active",
                )
            if con.execute(
                "SELECT 1 FROM sprint_cancellations WHERE sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone() is not None:
                return 409, _err_obj(
                    "sprint_already_cancelled",
                    f"sprint {sprint_doc_id} already has a cancellation request",
                )
            if sprint["planner_shell_id"] is None \
                    or sprint["flavor"] != "planner":
                return 409, _err_obj(
                    "planner_unavailable",
                    f"sprint {sprint_doc_id} has no active originating Planner",
                )
            planner = {
                "shell_id": sprint["planner_shell_id"],
                "shortname": sprint["shortname"],
                "display_name": sprint["display_name"],
                "flavor": sprint["flavor"],
            }
            try:
                harness, model = sprint_lifecycle.split_route(
                    sprint["planner_route"]
                )
            except sprint_lifecycle.SprintLifecycleError as exc:
                return 409, _err_obj("planner_route_invalid", str(exc))
            route, route_error = _conversation_route(
                con, planner, harness, model
            )
            if route_error is not None:
                return 422, route_error
            cancelled_conversations, active_runs = (
                _cancel_existing_sprint_work(con, sprint_doc_id)
            )
            directive_id = int(
                con.execute(
                    "INSERT INTO directives "
                    "(issuer_shell_id,issuer_flavor,kind,payload,target,"
                    "sprint_doc_id,status,executed_at) "
                    "VALUES (NULL,'system','cancel',?,'planner',?,"
                    "'executed',datetime('now'))",
                    (
                        json.dumps({"reason": reason}, sort_keys=True),
                        sprint_doc_id,
                    ),
                ).lastrowid
            )
            planner_conversation_id = _create_sprint_conversation(
                con,
                sprint_doc_id=sprint_doc_id,
                shell=planner,
                role="planner",
                lifecycle="one_shot",
                route=route,
                title=f"Sprint #{sprint_doc_id} abort report",
                creation_key=f"sprint-cancel:{sprint_doc_id}",
                source_directive_id=directive_id,
                required_result_kind="abort-report",
                prompt=(
                    f"The operator cancelled Sprint #{sprint_doc_id}. "
                    f"Reason: {reason}\n\n"
                    "Inspect the durable Sprint board and history, write the "
                    "abort report, then close it with `sc sprint abort "
                    f"--sprint {sprint_doc_id} --report-file <path>`. You are "
                    "the originating Planner and the only actor authorized "
                    "to complete this abort."
                ),
            )
            cancellation_id = int(
                con.execute(
                    "INSERT INTO sprint_cancellations "
                    "(sprint_doc_id,requested_by_user_id,reason,"
                    "source_directive_id,planner_conversation_id) "
                    "VALUES (?,?,?,?,?)",
                    (
                        sprint_doc_id,
                        user["user_id"],
                        reason,
                        directive_id,
                        planner_conversation_id,
                    ),
                ).lastrowid
            )
            con.execute(
                "INSERT INTO sentinel_events "
                "(event_kind,sprint_doc_id,directive_id,evidence) "
                "VALUES ('sprint-cancel-requested',?,?,?)",
                (
                    sprint_doc_id,
                    directive_id,
                    json.dumps(
                        {
                            "cancellation_id": cancellation_id,
                            "reason": reason,
                        },
                        sort_keys=True,
                    ),
                ),
            )
            notify_ids.extend(cancelled_conversations)
            notify_ids.append(planner_conversation_id)
            interrupt_ids.extend(active_runs)
            return 202, {
                "cleared": True,
                "cancellation": _cancellation_projection(
                    con, sprint_doc_id
                ),
            }

        response = _idempotent_atomic(
            con,
            actor,
            f"sprint_cancel:{sprint_doc_id}",
            headers,
            normalized,
            produce,
            response_headers=location,
        )
    finally:
        con.close()
    for conversation_id in set(notify_ids):
        conversation_events.notify(conversation_id)
    for run_id in interrupt_ids:
        try:
            conversation_broker.interrupt_run(run_id)
        except conversation_broker.BrokerError:
            # The interrupt request is already durable. A terminal race or an
            # offline broker is reconciled from that event on its next cycle.
            pass
    if notify_ids or interrupt_ids:
        conversation_broker.notify_commit()
    return response


def _abort_sprint(
    actor: _Actor,
    headers,
    sprint_doc_id: int,
    body: dict,
):
    bad = _unknown_fields(body, _ABORT_FIELDS, "abort")
    if bad is not None:
        return bad
    if body.get("state") != "aborted":
        return _err(
            422,
            "validation",
            "Planner abort requires exactly state=aborted plus report",
        )
    report = body.get("report")
    if not isinstance(report, str) or not report.strip():
        return _err(422, "validation", "report must be a nonblank string")
    report = report.strip()
    if len(report) > 1048576:
        return _err(
            422, "validation", "report must be at most 1048576 characters"
        )
    normalized = {"state": "aborted", "report": report}
    con = _db()
    notify_ids: list[str] = []
    try:
        flavor = _actor_flavor(con, actor)
        if flavor is None or flavor[0] != "planner":
            return _err(
                403,
                "planner_required",
                "Sprint abort completion requires an active Planner token",
            )

        def produce():
            sprint = con.execute(
                "SELECT state,planner_shell_id FROM sprints "
                "WHERE sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone()
            if sprint is None:
                return 404, _err_obj(
                    "not_found", f"no sprint declaration {sprint_doc_id}"
                )
            if sprint["planner_shell_id"] != actor.shell_id:
                return 403, _err_obj(
                    "not_sprint_owner",
                    f"shell {actor.shell_id} is not sprint "
                    f"{sprint_doc_id}'s originating Planner",
                )
            cancellation = con.execute(
                "SELECT cancellation_id,state FROM sprint_cancellations "
                "WHERE sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone()
            if cancellation is None or cancellation["state"] != "requested":
                return 409, _err_obj(
                    "cancellation_required",
                    f"sprint {sprint_doc_id} has no open cancellation request",
                )
            if sprint["state"] not in ("declared", "active"):
                return 409, _err_obj(
                    "sprint_not_abortable",
                    f"sprint {sprint_doc_id} is {sprint['state']}",
                )
            con.execute(
                "UPDATE sprint_cancellations SET state='completed',"
                "abort_report=?,completed_by_shell_id=?,"
                "completed_at=datetime('now') WHERE cancellation_id=?",
                (report, actor.shell_id, cancellation["cancellation_id"]),
            )
            sprint_lifecycle.transition(con, sprint_doc_id, "aborted")
            conductor_id = sprint_conversations.request_conductor_close(
                con,
                sprint_doc_id,
                reason="originating Planner completed the abort report",
            )
            if conductor_id is not None:
                notify_ids.append(conductor_id)
            con.execute(
                "UPDATE documents SET frozen=1,frozen_date=date('now'),"
                "updated_at=datetime('now') WHERE document_id=?",
                (sprint_doc_id,),
            )
            con.execute(
                "INSERT INTO sentinel_events "
                "(event_kind,shell_id,sprint_doc_id,evidence) "
                "VALUES ('sprint-aborted',?,?,?)",
                (
                    actor.shell_id,
                    sprint_doc_id,
                    json.dumps(
                        {
                            "cancellation_id": cancellation["cancellation_id"],
                            "report_sha256": hashlib.sha256(
                                report.encode()
                            ).hexdigest(),
                        },
                        sort_keys=True,
                    ),
                ),
            )
            notify_ids.extend(
                row[0]
                for row in con.execute(
                    "SELECT conversation_id FROM conversations "
                    "WHERE mode='sprint' AND sprint_doc_id=?",
                    (sprint_doc_id,),
                )
            )
            return 200, _sprint_projection(con, sprint_doc_id)

        response = _idempotent_atomic(
            con,
            actor,
            f"sprint_abort:{sprint_doc_id}",
            headers,
            normalized,
            produce,
        )
    finally:
        con.close()
    for conversation_id in set(notify_ids):
        conversation_events.notify(conversation_id)
    return response


def _resolve_planner(con, value):
    try:
        shell_id = _resolve_shell(con, value)
    except _BadShell as exc:
        return None, str(exc)
    row = con.execute(
        "SELECT shell_id FROM shells WHERE shell_id=? AND flavor='planner' "
        "AND COALESCE(is_deleted,0)=0",
        (shell_id,),
    ).fetchone()
    if row is None:
        return None, f"shell {value!r} is not an active Planner"
    return shell_id, None


def _adopt_sprint(actor: _Actor, headers, sprint_doc_id: int, body: dict):
    if actor.kind != "operator":
        return _err(
            403,
            "operator_required",
            "legacy sprint adoption is operator-only",
        )
    bad = _unknown_fields(body, _ADOPT_FIELDS, "adoption")
    if bad is not None:
        return bad
    if not _is_int(body.get("spec_doc_id")):
        return _err(422, "validation", "spec_doc_id must be an integer")
    evidence = body.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return _err(422, "validation", "evidence must be a nonblank string")
    con = _db()
    try:
        planner_id, error = _resolve_planner(con, body.get("planner"))
        if error is not None:
            return _err(422, "invalid_planner", error)
        def produce():
            routes = {}
            for field in ("planner_route", "dev_route", "reviewer_route"):
                route, route_error = _validate_route(
                    con, body.get(field), field
                )
                if route_error is not None:
                    return 422, route_error
                routes[field] = route
            sprint = con.execute(
                "SELECT sp.state,sp.legacy,d.frozen "
                "FROM sprints sp JOIN documents d "
                "ON d.document_id=sp.sprint_doc_id "
                "WHERE sp.sprint_doc_id=?",
                (sprint_doc_id,),
            ).fetchone()
            if sprint is None:
                return 404, _err_obj(
                    "not_found", f"no sprint declaration {sprint_doc_id}"
                )
            if sprint["state"] != "needs_owner" or not sprint["legacy"]:
                return 409, _err_obj(
                    "not_adoptable",
                    f"sprint {sprint_doc_id} is not a migrated needs_owner board",
                )
            spec = con.execute(
                "SELECT 1 FROM documents WHERE document_id=? AND kind='spec'",
                (body["spec_doc_id"],),
            ).fetchone()
            if spec is None:
                return 422, _err_obj(
                    "not_a_spec",
                    f"document {body['spec_doc_id']} is not a spec",
                )
            state = "closed" if sprint["frozen"] else "declared"
            con.execute(
                "UPDATE sprints SET spec_doc_id=?,planner_shell_id=?,"
                "planner_route=?,dev_route=?,reviewer_route=?,state=?,"
                "closed_at=CASE WHEN ?='closed' THEN datetime('now') "
                "ELSE closed_at END WHERE sprint_doc_id=?",
                (
                    body["spec_doc_id"],
                    planner_id,
                    routes["planner_route"],
                    routes["dev_route"],
                    routes["reviewer_route"],
                    state,
                    state,
                    sprint_doc_id,
                ),
            )
            con.execute(
                "INSERT INTO sentinel_events "
                "(event_kind,shell_id,sprint_doc_id,evidence) "
                "VALUES ('sprint-adopted',?,?,?)",
                (
                    planner_id,
                    sprint_doc_id,
                    json.dumps(
                        {"evidence": evidence.strip(), "state": state},
                        sort_keys=True,
                    ),
                ),
            )
            return 200, _sprint_projection(con, sprint_doc_id)

        return _idempotent_atomic(
            con,
            actor,
            f"sprint_adopt:{sprint_doc_id}",
            headers,
            body,
            produce,
        )
    finally:
        con.close()


def _list_sprints(query: dict):
    state = query.get("status", [None])[0]
    view = query.get("view", [None])[0]
    if view is not None:
        if view != "board" or state is not None:
            return _err(
                422,
                "validation",
                "view must be board and cannot be combined with status",
            )
        raw_recent = query.get("recent", ["5"])[0]
        try:
            recent = int(raw_recent)
        except (TypeError, ValueError):
            recent = -1
        if recent < 0 or recent > 20:
            return _err(
                422,
                "validation",
                "recent must be an integer from 0 through 20",
            )
        con = _db()
        try:
            return _json(200, _sprint_overview(con, recent))
        finally:
            con.close()
    if state is not None and state not in sprint_lifecycle.SPRINT_STATES:
        return _err(
            422,
            "validation",
            "status must be one of " + ", ".join(sprint_lifecycle.SPRINT_STATES),
        )
    con = _db()
    try:
        sql = "SELECT sprint_doc_id FROM sprints"
        params = ()
        if state is not None:
            sql += " WHERE state=?"
            params = (state,)
            if state == "active":
                sql += (
                    " AND NOT EXISTS ("
                    " SELECT 1 FROM sprint_cancellations sc "
                    " WHERE sc.sprint_doc_id=sprints.sprint_doc_id)"
                )
        sql += " ORDER BY sprint_doc_id"
        sprints = [
            _sprint_projection(con, row[0])
            for row in con.execute(sql, params)
        ]
        return _json(
            200,
            {"active_count": sum(s["state"] == "active" for s in sprints),
             "sprints": sprints},
        )
    finally:
        con.close()


def _get_sprint(sprint_doc_id: int):
    con = _db()
    try:
        sprint = _sprint_projection(con, sprint_doc_id)
        return _json(200, sprint) if sprint else _err(
            404, "not_found", f"no sprint declaration {sprint_doc_id}"
        )
    finally:
        con.close()


def _get_sprint_cancellation(
    sprint_doc_id: int,
    cancellation_id: int | None = None,
):
    con = _db()
    try:
        cancellation = _cancellation_projection(con, sprint_doc_id)
        if cancellation is None or (
            cancellation_id is not None
            and cancellation["cancellation_id"] != cancellation_id
        ):
            return _err(
                404,
                "not_found",
                f"no cancellation resource for sprint {sprint_doc_id}",
            )
        return _json(200, cancellation)
    finally:
        con.close()


def _add_sprint_unit(actor, headers, body):
    """POST /api/sprint-units — declare one unit on a sprint's board.

    Deliberately NOT an upsert, and its counterpart PATCH is deliberately not
    a create: the natural key is (sprint_doc_id, seq) typed by hand, so a
    typo'd seq on an edit must fail rather than mint a phantom unit that the
    reconciler then expects a shell to be working on.
    """
    doc_id = body.get("sprint_doc_id")
    seq = body.get("seq")
    if not _is_int(doc_id) or not isinstance(seq, str) or not seq.strip():
        return _err(422, "validation",
                    "sprint_doc_id (int) and seq (non-empty str, e.g. 'U1') "
                    "required")
    seq = seq.strip()
    bad = _bad_unit_field(body)
    if bad is not None:
        return bad
    if "unit_title" not in body:
        return _err(422, "validation", "unit_title (non-empty str) required")
    title = body["unit_title"].strip()
    state = body.get("state", "pending")
    if state not in _UNIT_STATES:
        return _err(422, "validation",
                    f"state must be one of {', '.join(_UNIT_STATES)}")
    con = _db()
    try:
        refusal = _may_write_board(con, actor, doc_id)
        if refusal is not None:
            return refusal

        def produce():
            # "A sprint document" has ONE definition in this engine —
            # `sprint_state` — and this route uses it rather than a second
            # clause that can drift from it.
            #
            # `is_writable_sprint_board`, not `is_live_sprint`: a frozen board
            # takes no NEW units (H-1 answers the question this site used to
            # park), but the unit-count clause cannot appear here, because this
            # is the route that CREATES the first unit.
            #
            # "No new units" is narrower than "immutable", deliberately: PATCH
            # is NOT gated on the freeze, so a planner can still correct a
            # closed sprint's board. You may correct history, not extend it.
            #
            # The typo this catches is adjacent-integer, not exotic: a sprint
            # doc and its spec are consecutive ids (59 and 58 for this very
            # sprint). A board minted on the spec is invisible to every
            # participant — they read the sprint doc — while the rows exist
            # and the reconciler watches them.
            doc = con.execute(
                "SELECT title, frozen FROM documents WHERE document_id=?",
                (doc_id,)).fetchone()
            if doc is None:
                return 404, _err_obj("no_such_sprint",
                                     f"no document {doc_id} to hold a board")
            if not sprint_state.is_writable_sprint_board(con, doc_id):
                if doc[1] and sprint_state.is_sprint_doc(con, doc_id):
                    return 409, _err_obj(
                        "sprint_frozen",
                        f"sprint {doc_id} ({doc[0]!r}) is frozen — a frozen "
                        "sprint is closed, and a closed board takes no writes")
                return 422, _err_obj(
                    "not_a_sprint_doc",
                    f"document {doc_id} is {doc[0]!r}, not a sprint board — "
                    "a board declared here is invisible to every participant "
                    "(they read the sprint doc) while its units are watched "
                    "all the same; check the --sprint id")
            try:
                roles = {
                    col: _resolve_role_shell(con, body.get(role), role)
                         for role, col in _UNIT_ROLES.items()}
            except _BadShell as exc:
                return 422, _err_obj("no_such_shell", str(exc))
            try:
                cur = con.execute(
                    "INSERT INTO sprint_units "
                    "(sprint_doc_id, seq, unit_title, dev_shell_id, "
                    " reviewer_shell_id, state, depends_on, overlap, branch, "
                    " pr_number, review_head, assigned_at, "
                    " updated_by_shell_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, seq, title, roles["dev_shell_id"],
                     roles["reviewer_shell_id"], state,
                     body.get("depends_on"), body.get("overlap"),
                     body.get("branch"), body.get("pr_number"),
                     body.get("review_head"),
                     _now(con) if any(roles.values()) else None,
                     actor.shell_id))
            except db_driver.IntegrityError:
                claimed = _pr_claimed_by(con, doc_id, body.get("pr_number"),
                                         seq)
                if claimed is not None:
                    return 409, claimed
                return 409, _err_obj(
                    "unit_exists",
                    f"sprint {doc_id} already has unit {seq} — edit it with "
                    "PATCH rather than declaring it twice")
            # A declaration that names a role IS an assignment change: before
            # is the empty board this row did not exist on.
            _emit_assignment_notice(con, actor, doc_id, seq,
                                    {c: None for c in _ROLE_COLS}, roles)
            con.commit()
            _log(f"sprint board: doc={doc_id} unit={seq} declared "
                 f"by={actor.scope}")
            return 201, _unit_projection(con, cur.lastrowid)

        return _idempotent(con, actor, "sprint_unit_add", headers, body,
                           produce)
    finally:
        con.close()


def _patch_sprint_unit(actor, headers, body):
    """PATCH /api/sprint-units — move one unit, addressed by its natural key
    (sprint_doc_id, seq) in the body rather than a surrogate id in the path.
    The planner types the key it reads off the board; resolving seq→unit_id
    in the client would cost a second round trip that another planner edit
    can interleave with.

    A `state` move may not ride along with field edits. State is the only
    column the reconciler derives ROLE EXPECTATION from, so it gets its own
    call and `state_changed_at` gets exactly one writer — a planner can never
    move what a worker is expected to be doing as a side effect of fixing a
    branch name. `sc sprint unit state` is that call; the refusal here is
    what makes the CLI's separation a property rather than a convention.
    """
    doc_id = body.get("sprint_doc_id")
    seq = body.get("seq")
    if not _is_int(doc_id) or not isinstance(seq, str) or not seq.strip():
        return _err(422, "validation",
                    "sprint_doc_id (int) and seq (str) address the unit")
    seq = seq.strip()
    bad = _bad_unit_field(body)
    if bad is not None:
        return bad
    edits = {f: body[f] for f in _UNIT_FIELDS if f in body}
    roles = {r: body[r] for r in _UNIT_ROLES if r in body}
    state = body.get("state")
    if state is not None and (edits or roles):
        return _err(422, "state_moves_alone",
                    "a state move takes no other edits — move the state in "
                    "its own call so state_changed_at has one writer")
    if state is not None and state not in _UNIT_STATES:
        return _err(422, "validation",
                    f"state must be one of {', '.join(_UNIT_STATES)}")
    if state is None and not edits and not roles:
        return _err(422, "validation", "no fields to change")
    con = _db()
    try:
        refusal = _may_write_board(con, actor, doc_id)
        if refusal is not None:
            return refusal

        def produce():
            row = con.execute(
                "SELECT unit_id, state, dev_shell_id, reviewer_shell_id "
                "FROM sprint_units WHERE sprint_doc_id=? AND seq=?",
                (doc_id, seq)).fetchone()
            if row is None:
                return 404, _err_obj(
                    "no_such_unit",
                    f"sprint {doc_id} has no unit {seq!r} — declare it with "
                    "POST; an edit never creates one")
            unit_id, was_state, was_dev, was_rev = row
            sets, params = [], []
            if state is not None:
                try:
                    _check_transition(SPRINT_UNIT_EDGES, was_state, state)
                except SprintTransitionError:
                    return 409, _err_obj(
                        "illegal_unit_transition", _no_such_move(
                            doc_id, seq, was_state, state))
                sets.append("state=?")
                params.append(state)
                if state != was_state:
                    # Only a REAL move restamps the clock. The no-progress
                    # window resets on state change, so a re-assert of the
                    # same state must not silently grant a stalled worker
                    # another full window.
                    sets.append("state_changed_at=datetime('now')")
            try:
                resolved = {
                    _UNIT_ROLES[r]: _resolve_role_shell(con, v, r)
                    for r, v in roles.items()
                }
            except _BadShell as exc:
                return 422, _err_obj("no_such_shell", str(exc))
            for col, val in resolved.items():
                sets.append(f"{col}=?")
                params.append(val)
            was = {"dev_shell_id": was_dev, "reviewer_shell_id": was_rev}
            if any(was[c] != v for c, v in resolved.items()):
                sets.append("assigned_at=datetime('now')")
            for field in _UNIT_FIELDS:
                if field in edits:
                    sets.append(f"{field}=?")
                    params.append(edits[field])
            sets.append("updated_at=datetime('now')")
            sets.append("updated_by_shell_id=?")
            params.append(actor.shell_id)
            try:
                con.execute(
                    f"UPDATE sprint_units SET {', '.join(sets)} "
                    "WHERE unit_id=?", (*params, unit_id))
            except db_driver.IntegrityError:
                claimed = _pr_claimed_by(con, doc_id, edits.get("pr_number"),
                                         seq)
                if claimed is not None:
                    return 409, claimed
                raise
            # A role omitted from the body is left alone, so `after` is the
            # row as it now reads — not the request. The counterpart the
            # notice names has to be the board's, not the caller's subset.
            _emit_assignment_notice(con, actor, doc_id, seq, was,
                                    {**was, **resolved})
            con.commit()
            _log(f"sprint board: doc={doc_id} unit={seq} moved "
                 f"by={actor.scope} ({'state ' + state if state else 'fields'})")
            return 200, _unit_projection(con, unit_id)

        return _idempotent(con, actor, "sprint_unit_patch", headers, body,
                           produce)
    finally:
        con.close()


# ------------------------------------------------------------------ dispatch

def handle(method: str, path: str, headers_raw: str, body: bytes) -> tuple:
    headers = _parse_headers(headers_raw)
    if not _host_ok(headers):
        return _err(403, "host_not_allowed",
                    "sprint board API serves 127.0.0.1/localhost only")
    u = urlparse(path)
    p = u.path
    query = parse_qs(u.query)
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return _err(400, "bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        return _err(400, "bad_json", "request body must be a JSON object")
    actor = _resolve_actor(headers)
    if actor is None:
        return _err(401, "unauthorized",
                    "the presented Bearer token matches no shell")
    if method in ("POST", "DELETE", "PATCH", "PUT") \
            and not _mutation_site_ok(headers):
        return _err(403, "not_same_origin",
                    "cross-site board mutation rejected")
    if p == "/api/sprint-units" and method == "GET":
        return _sprint_units(actor, query)
    if p == "/api/sprint-units" and method == "POST":
        return _add_sprint_unit(actor, headers, data)
    if p == "/api/sprint-units" and method == "PATCH":
        return _patch_sprint_unit(actor, headers, data)
    if p == "/api/spec-qaqc-reviews" and method == "GET":
        return _list_qaqc(query)
    if p == "/api/spec-qaqc-reviews" and method == "POST":
        return _create_qaqc(actor, headers, data)
    if p == "/api/sprints" and method == "GET":
        return _list_sprints(query)
    if p == "/api/sprints" and method == "POST":
        return _create_sprint(actor, headers, data)
    if p.startswith("/api/sprints/"):
        parts = p.strip("/").split("/")
        if len(parts) not in (3, 4, 5):
            return _err(404, "no_such_route", f"no route: {method} {p}")
        try:
            sprint_doc_id = int(parts[2])
        except ValueError:
            return _err(422, "validation", "sprint id must be an integer")
        if len(parts) == 3 and method == "GET":
            return _get_sprint(sprint_doc_id)
        if len(parts) == 3 and method == "PATCH":
            if data.get("state") == "active":
                return _arm_sprint(actor, headers, sprint_doc_id, data)
            if data.get("state") == "aborted":
                return _abort_sprint(actor, headers, sprint_doc_id, data)
            return _err(
                422,
                "validation",
                "Planner Sprint state update must request active or aborted",
            )
        if len(parts) == 4 and parts[3] == "adopt" and method == "POST":
            return _adopt_sprint(actor, headers, sprint_doc_id, data)
        if len(parts) == 4 and parts[3] == "cancellations" \
                and method == "POST":
            return _cancel_sprint(
                actor, headers, sprint_doc_id, data
            )
        if len(parts) == 4 and parts[3] == "cancellations" \
                and method == "GET":
            return _get_sprint_cancellation(sprint_doc_id)
        if len(parts) == 5 and parts[3] == "cancellations" \
                and method == "GET":
            try:
                cancellation_id = int(parts[4])
            except ValueError:
                return _err(
                    422,
                    "validation",
                    "cancellation id must be an integer",
                )
            return _get_sprint_cancellation(
                sprint_doc_id, cancellation_id
            )
    return _err(404, "no_such_route", f"no route: {method} {p}")
