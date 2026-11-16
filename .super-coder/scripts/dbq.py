#!/usr/bin/env python3
"""Host application-DB broker and socket client.

The host owns the DSN; shells submit SQL over the shared unix socket. Config
selects read_only (legacy default) or read_write access. Read/write mode executes
SQL unchanged, including diagnostics, DDL, repairs, and transaction scripts.
A request commits on success and rolls back on failure unless the SQL explicitly
controls its own transactions or autocommit is requested for maintenance.
Persistent host env-file settings take precedence over inherited environment.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import shlex
import sys
from pathlib import Path

import ports

# The broker listens here — a unix socket inside the bind-mounted engine dir, so
# the same absolute path resolves on the host (where the broker runs) and in the
# sandbox (where a shell's socket client reaches it). No network surface; fs-perm gated.
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
    if db:
        ports.update({"db": db})
    else:
        ports.update({}, remove=("db",))
    return db


def _policy(cfg: dict) -> dict:
    """Resolve the effective (defaulted) policy from a `db` block."""
    return {
        "dsn_env": cfg.get("dsn_env") or DEFAULT_DSN_ENV,
        "allow_tables": [t.lower() for t in (cfg.get("allow_tables") or DEFAULT_ALLOW_TABLES)],
        "row_cap": int(cfg.get("row_cap") or DEFAULT_ROW_CAP),
        "statement_timeout_ms": int(cfg.get("statement_timeout_ms") or DEFAULT_TIMEOUT_MS),
        "access_mode": cfg.get("access_mode", "read_only"),
        "dsn_env_file": cfg.get("dsn_env_file"),
    }


# -- Optional legacy read-only policy ---------------------------------------

# Match tokens in one pass: comment markers inside a literal are data, and
# quote characters inside a comment do not begin a literal. This text is only
# for the policy scan; PostgreSQL always receives the original SQL.
_IGNORED_SQL = re.compile(
    r"[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'|"
    r"\$(?P<tag>[A-Za-z_][A-Za-z_0-9]*|)\$.*?\$(?P=tag)\$|"
    r"--[^\n]*|/\*.*?\*/", re.S,
)
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
    return _IGNORED_SQL.sub(" ", sql)


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
    """Apply the configured SQL policy without changing the executed text."""
    raw = (sql or "").strip()
    if not raw:
        return False, "empty query"
    pol = _policy(cfg)
    if pol["access_mode"] == "read_write":
        return True, "ok"
    if pol["access_mode"] != "read_only":
        return False, "access_mode must be read_only or read_write"
    stripped = _strip(raw)
    # Single statement only — no stacked statements. A trailing ';' is fine; any
    # ';' with more query after it is a second statement → reject.
    body = stripped.rstrip().rstrip(";")
    if ";" in body:
        return False, "only a single statement is allowed (no stacked statements)"
    if not re.match(r"^\s*(select|with|explain|show)\b", body, re.I):
        return False, "read_only mode accepts SELECT, WITH, EXPLAIN, and SHOW"
    if _FORBIDDEN.search(body):
        bad = _FORBIDDEN.search(body).group(1).upper()
        return False, f"disallowed keyword '{bad}' — this is a read-only surface"
    tables = _tables(body)
    unlisted = [] if "*" in pol["allow_tables"] else [t for t in tables if t not in pol["allow_tables"]]
    if unlisted:
        return False, (f"table(s) {unlisted} not in the allowlist "
                       f"{pol['allow_tables']} — ask the operator to widen the "
                       "`db` block's allow_tables (and the RO role's GRANTs)")
    return True, "ok"


# -- DSN resolution (host-side only; never on argv) ---------------------------

def resolve_dsn(pol: dict) -> tuple[str | None, dict, str | None]:
    """Resolve the chosen credential without executing shell code or using argv."""
    var = pol["dsn_env"]
    env_file = Path(pol.get("dsn_env_file") or os.environ.get("SC_RO_ENVFILE")
                    or Path.home() / ".config" / ports.REPO_ROOT.name / "db-broker.env")
    dsn = os.environ.get(var)
    try:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                key, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and key.strip() == var:
                    # Both single/double quoted systemd assignments are accepted;
                    # interpolation and command substitution are never performed.
                    parts = shlex.split(value, comments=True)
                    dsn = " ".join(parts)
    except (OSError, ValueError):
        return None, {}, f"cannot read {var} from the broker host env file"
    if not dsn:
        return None, {}, f"{var} is not set in the broker host env file or environment"
    return dsn, {}, None


def _cell(value):
    """Keep the existing string-cell protocol while preserving SQL NULL."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def do_query(sql: str, *, autocommit: bool = False) -> dict:
    """Execute the original SQL; return each statement's result and commit status.

    PostgreSQL parses SQL, so strings, comments, dollar quotes and WITH clauses
    retain their meaning. Requests own separate connections. A failed ordinary
    request rolls back all its statements, and is never retried automatically.
    """
    cfg = read()
    if cfg is None:
        return {"ok": False, "error": "no `db` block in instance.json — run `./sc db-init`"}
    if not isinstance(sql, str) or not isinstance(autocommit, bool):
        return {"ok": False, "error": "sql must be a string and autocommit must be a boolean"}
    try:
        pol = _policy(cfg)
        ok, reason = check(sql, cfg)
    except (TypeError, ValueError, AttributeError):
        return {"ok": False, "error": "invalid database broker policy"}
    if not ok:
        return {"ok": False, "error": reason}
    dsn, _, error = resolve_dsn(pol)
    if error:
        return {"ok": False, "error": error}
    try:
        import psycopg
    except ImportError:
        return {"ok": False, "error": "broker driver missing; run ./sc db-broker-install on the host"}
    cap = max(1, pol["row_cap"])
    options = (f"-c statement_timeout={max(1, pol['statement_timeout_ms'])} "
               f"-c lock_timeout={min(5000, max(1, pol['statement_timeout_ms']))} "
               "-c idle_in_transaction_session_timeout=30000 "
               f"-c default_transaction_read_only={'on' if pol['access_mode'] == 'read_only' else 'off'}")
    try:
        results = []
        with psycopg.connect(dsn, connect_timeout=3, options=options,
                             application_name="subfloor-db-broker",
                             autocommit=autocommit) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                while True:
                    columns = [c.name for c in cur.description] if cur.description else []
                    rows = cur.fetchmany(cap + 1) if cur.description else []
                    results.append({
                        "columns": columns,
                        "rows": [[_cell(v) for v in row] for row in rows[:cap]],
                        "row_count": min(len(rows), cap),
                        "truncated": len(rows) > cap,
                        "affected_rows": cur.rowcount,
                        "status": cur.statusmessage,
                    })
                    if not cur.nextset():
                        break
        result = {"ok": True, **results[-1], "autocommit": autocommit}
        if len(results) > 1:
            result["results"] = results
        return result
    except psycopg.Error as exc:
        # Primary server errors describe SQL failures without echoing credentials.
        message = exc.diag.message_primary or str(exc)
        message = message.replace(dsn, "[connection redacted]")
        try:
            password = psycopg.conninfo.conninfo_to_dict(dsn).get("password")
            if password:
                message = message.replace(password, "[redacted]")
        except psycopg.Error:
            message = "invalid database connection configuration"
        return {"ok": False, "error": message, "sqlstate": exc.sqlstate,
                "autocommit": autocommit}
    except (ValueError, TypeError):
        return {"ok": False, "error": "invalid database connection configuration"}


def health() -> dict:
    result = do_query("SELECT current_database(), current_user, current_setting('transaction_read_only')")
    if not result.get("ok"):
        return {"ok": False, "service": "db-broker", "error": result.get("error")}
    database, role, read_only = result["rows"][0]
    return {"ok": True, "service": "db-broker", "database": database, "role": role,
            "access_mode": _policy(read() or {})["access_mode"],
            "transaction_read_only": read_only}


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
    parser = argparse.ArgumentParser(description="Query the live application DB through its host broker")
    parser.add_argument("mode", choices=("sock", "configured", "check", "query", "health"), nargs="?", default="sock")
    parser.add_argument("sql", nargs="?", help="SQL text, or - to read stdin")
    parser.add_argument("--file", help="read SQL from this file")
    parser.add_argument("--autocommit", action="store_true", help="allow commands such as VACUUM outside a transaction")
    args = parser.parse_intermixed_args(argv)
    if args.mode == "sock":
        print(SOCKET)
        return 0
    if args.mode == "configured":
        return 0 if read() else 1
    if args.sql is not None and args.file:
        parser.error("use SQL text or --file, not both")
    try:
        sql = Path(args.file).read_text() if args.file else sys.stdin.read() if args.sql == "-" else args.sql or ""
        if args.mode == "health":
            result = broker_call("GET", "/health")
        elif args.mode == "check":
            ok, reason = check(sql, read() or {})
            result = {"ok": ok, "reason": reason}
        else:
            result = broker_call("POST", "/query", {"sql": sql, "autocommit": args.autocommit})
    except (OSError, ConnectionError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
