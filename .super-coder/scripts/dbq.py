#!/usr/bin/env python3
"""Host db-broker — read-only diagnostic access to the fork's LIVE app DB.

Link-only, read-only, host-side by design. A sandbox shell diagnosing live app
behaviour cannot reach the fork's live Postgres: `./sc launch` gives it an
*empty* private sidecar (dev/test target), the host DSN is never mounted, and
the live stack sits on a network the container has no route to. So a shell must
hand the operator a SQL block and wait for a paste-back. This broker closes that
loop for confirmation-grade reads — telemetry/ops tables a shell needs to
confirm a diagnosis it has already reasoned out — without ever handing the
sandbox a credential or a route.

It is the fourth sibling of the pm2 / Windows-VM / tailnet brokers
(api/pm2_broker.py, api/vm_broker.py, api/ts_broker.py): one host process holds
the capability so nothing downstream needs it. The broker (api/db_broker.py)
runs ON THE HOST, shells out to `psql` where the DSN + route resolve, and
exposes one narrow verb over a unix socket in the bind-mounted engine dir
(`.super-coder/run/db-broker.sock`). The `db_query` skill curls that socket.
Spec: specs_sc/db-query.md · doc: .super-coder/docs/db-broker.md.

The config lives under the `db` key of `.super-coder/instance.json`. Because the
whole repo is bind-mounted into the sandbox (`-v $here:$here`), instance.json is
sandbox-readable — so the block holds NO secret. It names an *env var*; the
broker (host-side) resolves the DSN from that var at query time. Non-secret
policy that IS safe to be sandbox-readable lives in the block:

    dsn_env               name of the host env var holding the read-only DSN
                          (default SC_RO_DSN) — the DSN itself never touches disk
    allow_tables          fail-closed allowlist of tables a query may touch
                          (default: ops/telemetry only — content tables are gated)
    row_cap               max rows returned (default 1000; truncation is flagged)
    statement_timeout_ms  server-side statement timeout (default 5000)
    psql_bin              path/name of the psql CLI (default "psql")

Read-only is enforced TWICE: (1) the DSN must point at a dedicated read-only
Postgres role (GRANT SELECT only) — DB-enforced, the backstop; the broker also
sets default_transaction_read_only=on on the session; and (2) the broker rejects
any statement that is not a single SELECT/WITH before psql ever runs. The
allowlist is likewise belt-and-suspenders: the RO role should GRANT SELECT only
on the allowlisted tables (DB-authoritative), and the broker additionally
rejects a query whose FROM/JOIN targets a table outside `allow_tables`.

Run on the HOST (never in the sandbox):
    ./sc db-broker        foreground
    ./sc db-broker-up     background (pidfile) ; ./sc db-broker-down to stop
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

import ports

# The broker listens here — a unix socket inside the bind-mounted engine dir, so
# the same absolute path resolves on the host (where the broker runs) and in the
# sandbox (where the db_query skill curls it). No network surface; fs-perm gated.
# Distinct filename from the sibling brokers' sockets so all four coexist.
RUN_DIR = ports.ENGINE / "run"
SOCKET = RUN_DIR / "db-broker.sock"

DEFAULT_ALLOW_TABLES = ["skill_runs", "tool_call_attempts", "models"]
DEFAULT_ROW_CAP = 1000
DEFAULT_TIMEOUT_MS = 5000
DEFAULT_DSN_ENV = "SC_RO_DSN"


# -- config (instance.json `db` block) ----------------------------------------

def read() -> dict | None:
    """The persisted db block, or None if the fork has not linked a live DB."""
    return ports.resolve(persist=False).get("db")


def write(db: dict | None) -> dict | None:
    """Persist (or clear) the db block, preserving every other config key
    (ports, and the pm2/vm/ts blocks — all coexist)."""
    cfg = ports.resolve(persist=False)
    if db:
        cfg["db"] = db
    else:
        cfg.pop("db", None)
    ports.save(cfg)
    return cfg.get("db")


def _policy(cfg: dict) -> dict:
    """Resolve the effective (defaulted) policy from a `db` block."""
    return {
        "dsn_env": cfg.get("dsn_env") or DEFAULT_DSN_ENV,
        "allow_tables": [t.lower() for t in (cfg.get("allow_tables") or DEFAULT_ALLOW_TABLES)],
        "row_cap": int(cfg.get("row_cap") or DEFAULT_ROW_CAP),
        "statement_timeout_ms": int(cfg.get("statement_timeout_ms") or DEFAULT_TIMEOUT_MS),
        "psql_bin": cfg.get("psql_bin") or "psql",
    }


# -- SELECT-only + allowlist validation (runs BEFORE psql) --------------------

_STRING_LIT = re.compile(r"'(?:[^']|'')*'")          # 'single-quoted', '' escapes
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# Mutating / DDL / session- and transaction-control verbs. A read path must
# reject every one of these, including inside a CTE (WITH x AS (INSERT ...)).
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|"
    r"merge|call|do|vacuum|reindex|refresh|comment|lock|reset|set|begin|"
    r"commit|rollback|savepoint|prepare|execute|deallocate|listen|notify|"
    r"unlisten|discard|cluster|attach|detach|import|load)\b",
    re.I,
)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.$]*)", re.I)
# CTE names defined by `WITH name AS (…)` (and each `, name AS (…)`). These are
# query-local, not real tables — the RO role never gates them, so the broker's
# allowlist must not either.
_CTE_NAME = re.compile(r"\b([a-zA-Z_]\w*)\s+as\s*\(", re.I)


def _strip(sql: str) -> str:
    """Remove string literals + comments so keyword/table scans don't trip on
    them (a column value 'please update the row' is not an UPDATE statement)."""
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    sql = _STRING_LIT.sub("''", sql)
    return sql


def _tables(stripped: str) -> list[str]:
    """Best-effort FROM/JOIN targets, schema-stripped + lowercased, minus any
    query-local CTE names. Defence in depth over the RO role's GRANTs — the role
    is the authoritative boundary."""
    ctes = {n.lower() for n in _CTE_NAME.findall(stripped)}
    out = []
    for ref in _TABLE_REF.findall(stripped):
        name = ref.split(".")[-1].lower()          # drop schema qualifier
        if name and name not in out and name not in ctes:
            out.append(name)
    return out


def check(sql: str, cfg: dict) -> tuple[bool, str]:
    """Validate a candidate query against the read-only + allowlist rules.
    Returns (ok, reason). Fail-closed: anything not provably a single, read-only,
    allowlisted SELECT is rejected."""
    raw = (sql or "").strip()
    if not raw:
        return False, "empty query"
    stripped = _strip(raw)
    # Single statement only — no stacked statements. A trailing ';' is fine; any
    # ';' with more query after it is a second statement → reject.
    body = stripped.rstrip().rstrip(";")
    if ";" in body:
        return False, "only a single statement is allowed (no stacked statements)"
    if not re.match(r"^\s*(select|with)\b", body, re.I):
        return False, "only SELECT (or WITH … SELECT) queries are allowed"
    if _FORBIDDEN.search(body):
        bad = _FORBIDDEN.search(body).group(1).upper()
        return False, f"disallowed keyword '{bad}' — this is a read-only surface"
    pol = _policy(cfg)
    tables = _tables(body)
    unlisted = [t for t in tables if t not in pol["allow_tables"]]
    if unlisted:
        return False, (f"table(s) {unlisted} not in the allowlist "
                       f"{pol['allow_tables']} — ask the operator to widen the "
                       "`db` block's allow_tables (and the RO role's GRANTs)")
    return True, "ok"


# -- DSN resolution (host-side only; never on argv) ---------------------------

def resolve_dsn(pol: dict) -> tuple[str | None, dict, str | None]:
    """Read the DSN from the host env var named by the block. Returns
    (dsn, libpq_env, error). The DSN is parsed into libpq PG* env vars so the
    password never lands on a process argv (visible in `ps`)."""
    var = pol["dsn_env"]
    dsn = os.environ.get(var)
    if not dsn:
        return None, {}, (f"the read-only DSN is not set: export ${var} in the "
                          "broker's host environment (it is never mounted into "
                          "the sandbox). See `./sc db-init`.")
    env: dict[str, str] = {}
    u = urllib.parse.urlparse(dsn)
    if u.scheme in ("postgres", "postgresql") and (u.hostname or u.path):
        if u.hostname:
            env["PGHOST"] = u.hostname
        if u.port:
            env["PGPORT"] = str(u.port)
        if u.username:
            env["PGUSER"] = urllib.parse.unquote(u.username)
        if u.password:
            env["PGPASSWORD"] = urllib.parse.unquote(u.password)
        db = (u.path or "").lstrip("/")
        if db:
            env["PGDATABASE"] = db
        return dsn, env, None
    # Not a URI (libpq key=value conninfo). We can't split it into PG* vars
    # cleanly; fall back to passing it on argv (host-side, trusted — the sandbox
    # has its own pid namespace and cannot see the host's `ps`).
    return dsn, {}, None


# -- the query verb (host-side; the broker exposes this over the socket) ------

def _run(argv: list[str], env: dict, timeout: int) -> tuple[int, str, str]:
    """Run psql with an augmented environment. The mock seam for tests (no live
    Postgres in CI) — sibling brokers mock their own `_run` the same way."""
    full = dict(os.environ)
    full.update(env)
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, env=full)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", f"command not found: {e.filename} — is psql installed on the host?"
    except subprocess.TimeoutExpired:
        return 124, "", f"psql timed out (>{timeout}s)"


def _parse_csv(out: str, cap: int) -> tuple[list[str], list[list[str]], bool]:
    """Split psql --csv output into (columns, rows, truncated). The wrap query
    fetches cap+1 rows so a full cap+1 signals there was more."""
    reader = list(csv.reader(io.StringIO(out)))
    if not reader:
        return [], [], False
    columns, data = reader[0], reader[1:]
    truncated = len(data) > cap
    return columns, data[:cap], truncated


def do_query(sql: str) -> dict:
    """Validate → run one SELECT against the live DB host-side → capped rows.
    Returns {ok, columns, rows, truncated} or {ok:false, error}."""
    cfg = read()
    if cfg is None:
        return {"ok": False, "error": "no `db` block in instance.json — run `./sc db-init`"}
    pol = _policy(cfg)
    ok, reason = check(sql, cfg)
    if not ok:
        return {"ok": False, "error": reason}
    dsn, pgenv, err = resolve_dsn(pol)
    if err:
        return {"ok": False, "error": err}
    cap = pol["row_cap"]
    inner = _strip(sql).strip().rstrip(";")  # already validated single-statement
    # Wrap to enforce the row cap in the DB; cap+1 so we can detect truncation.
    wrapped = f"SELECT * FROM ({inner}) AS _sc_q LIMIT {cap + 1}"
    # Session options via PGOPTIONS (applied at connect): a hard statement
    # timeout and a read-only transaction default — belt to the RO role's braces.
    pgenv = dict(pgenv)
    pgenv["PGOPTIONS"] = (f"-c statement_timeout={pol['statement_timeout_ms']} "
                          "-c default_transaction_read_only=on")
    argv = [pol["psql_bin"], "--csv", "-v", "ON_ERROR_STOP=1", "-w"]
    if not pgenv.get("PGHOST") and not pgenv.get("PGDATABASE") and dsn:
        argv += ["-d", dsn]  # non-URI conninfo fallback
    argv += ["-c", wrapped]
    timeout_s = max(2, pol["statement_timeout_ms"] // 1000 + 5)
    code, stdout, stderr = _run(argv, pgenv, timeout_s)
    if code != 0:
        return {"ok": False, "error": (stderr or stdout or "psql failed").strip()}
    columns, rows, truncated = _parse_csv(stdout, cap)
    return {"ok": True, "columns": columns, "rows": rows,
            "row_count": len(rows), "truncated": truncated}


# -- client: HTTP over the broker's unix socket -------------------------------

def broker_call(method: str, path: str, body: dict | None = None,
                timeout: int = 70) -> dict:
    """Speak HTTP/1.1 to the db-broker over its unix socket and return parsed
    JSON. Raises ConnectionError if the broker is not listening (so callers can
    render a 'start the broker' hint)."""
    payload = b"" if body is None else json.dumps(body).encode()
    req = (
        f"{method} {path} HTTP/1.1\r\nHost: db-broker\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + payload
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        s.connect(str(SOCKET))
        s.sendall(req)
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise ConnectionError(f"db-broker not reachable at {SOCKET}: {e}") from e
    finally:
        s.close()
    _, _, raw_body = b"".join(chunks).partition(b"\r\n\r\n")
    try:
        return json.loads(raw_body.decode() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad broker response",
                "raw": raw_body[:200].decode("latin1")}


# -- host CLI (path lookup for `sc`; verbs for manual no-broker testing) -------

def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "sock"
    if mode == "sock":
        print(SOCKET)
    elif mode == "configured":
        # exit 0 if this fork has linked a live DB (so the launch hook self-skips)
        return 0 if read() else 1
    elif mode == "check":
        # dbq.py check "<sql>" — validate only, no DB round-trip
        ok, reason = check(argv[1] if len(argv) > 1 else "", read() or {})
        print(json.dumps({"ok": ok, "reason": reason}))
    elif mode == "query":
        print(json.dumps(do_query(argv[1] if len(argv) > 1 else "")))
    else:
        sys.exit("usage: dbq.py [sock|configured|check <sql>|query <sql>]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
