#!/usr/bin/env python3
"""Thin database driver abstraction for super-coder.

Default: sqlite3 (zero extra deps).
Postgres: set DATABASE_URL=postgresql://user:pass@host/db to switch.

Exposes a sqlite3-compatible surface so every caller is dialect-agnostic.
Automatic SQL translations applied in postgres mode:
  ?                           → %s (parameter placeholders)
  datetime('now')             → CURRENT_TIMESTAMP
  date('now')                 → CURRENT_DATE
  PRAGMA journal_mode/...     → no-op
  PRAGMA table_info(t)        → information_schema.columns equivalent
  INSERT OR IGNORE            → INSERT ... ON CONFLICT DO NOTHING
  INSERT (w/o RETURNING)      → INSERT ... RETURNING * for lastrowid support
"""
from __future__ import annotations

import os
import re
from typing import Any

_URL: str = os.environ.get("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return bool(_URL)


def connect(path=None):
    """Return a DB connection.

    In sqlite3 mode path must be the .db file path.
    In postgres mode path is ignored; DATABASE_URL drives the connection.
    """
    if is_postgres():
        return _PgConn(_URL)
    import sqlite3
    con = sqlite3.connect(str(path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


# ── Exception aliases ─────────────────────────────────────────────────────────

if is_postgres():
    try:
        import psycopg as _pg
        IntegrityError = _pg.errors.IntegrityError
        OperationalError = _pg.OperationalError
    except ImportError:
        raise SystemExit(
            "DATABASE_URL is set but psycopg is not installed.\n"
            "  pip install 'psycopg[binary]'"
        )
else:
    import sqlite3 as _sq3
    IntegrityError = _sq3.IntegrityError
    OperationalError = _sq3.OperationalError


# ── SQL translation (postgres only) ──────────────────────────────────────────

_RE_NOOP_PRAGMA = re.compile(
    r"^PRAGMA[ \t]+(journal_mode|busy_timeout|foreign_keys|wal_autocheckpoint)"
    r"([ \t]*[=;][^;]*)?$",
    re.IGNORECASE,
)
# Applied to already-stripped input: no leading/trailing \s* needed.
# Avoids polynomial backtracking on inputs with many trailing spaces.
_RE_TABLE_INFO = re.compile(
    r"^PRAGMA[ \t]+table_info[ \t]*\([ \t]*(\w+)[ \t]*\);?$", re.IGNORECASE
)
_RE_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
_RE_INSERT = re.compile(r"^\s*INSERT\b", re.IGNORECASE)
_RE_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)


def _pg_translate(sql: str) -> tuple[str, bool]:
    """Translate one SQL statement for postgres.

    Returns (translated_sql, is_noop).
    is_noop=True means the caller should skip this statement entirely.
    """
    s = sql.strip()
    if not s:
        return "", True

    # Noop PRAgMAs
    if _RE_NOOP_PRAGMA.match(s):
        return "", True

    # PRAGMA table_info → information_schema (table name is in the SQL itself)
    m = _RE_TABLE_INFO.match(s)
    if m:
        table = m.group(1)
        pg = (
            "SELECT ordinal_position - 1 AS cid, column_name AS name, "
            "data_type AS type, "
            "(CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END) AS notnull, "
            "column_default AS dflt_value, 0 AS pk "
            "FROM information_schema.columns "
            f"WHERE table_name = '{table}' ORDER BY ordinal_position"
        )
        return pg, False

    # Normalize SQLite date/time functions (these work in both, but the SQLite
    # form isn't valid Postgres syntax)
    s = s.replace("datetime('now')", "CURRENT_TIMESTAMP")
    s = s.replace("date('now')", "CURRENT_DATE")

    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    had_ignore = bool(_RE_INSERT_OR_IGNORE.search(s))
    if had_ignore:
        s = _RE_INSERT_OR_IGNORE.sub("INSERT", s)

    # ? → %s parameter placeholders
    s = s.replace("?", "%s")

    # INSERT without RETURNING → append RETURNING * so lastrowid works
    is_insert = bool(_RE_INSERT.match(s))
    if is_insert and not _RE_RETURNING.search(s):
        body = s.rstrip().rstrip(";")
        if had_ignore:
            body += " ON CONFLICT DO NOTHING"
        s = body + " RETURNING *"
    elif had_ignore:
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    return s, False


# ── Row wrapper ───────────────────────────────────────────────────────────────

class _PgRow:
    """Dict-keyed, index-accessible row — mimics sqlite3.Row."""

    __slots__ = ("_names", "_vals")

    def __init__(self, description: Any, values: tuple) -> None:
        self._names: tuple = tuple(d[0] for d in description) if description else ()
        self._vals: tuple = values

    def __getitem__(self, key: int | str):
        if isinstance(key, int):
            val = self._vals[key]
        else:
            try:
                val = self._vals[self._names.index(key)]
            except ValueError:
                raise KeyError(key)
        # Coerce date/datetime back to ISO strings so callers see the same
        # text format they would from sqlite3.
        try:
            from datetime import date as _date, datetime as _dt
            if isinstance(val, _dt):
                return val.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(val, _date):
                return val.isoformat()
        except ImportError:
            pass
        return val

    def keys(self) -> tuple:
        return self._names

    def __iter__(self):
        return (self[i] for i in range(len(self._vals)))

    def __len__(self) -> int:
        return len(self._vals)

    def __repr__(self) -> str:
        return repr(dict(zip(self._names, self)))


# ── Cursor wrapper ────────────────────────────────────────────────────────────

class _PgCursor:
    """Wraps a psycopg cursor to look like sqlite3.Cursor."""

    def __init__(self, cur: Any, lastrowid: Any = None) -> None:
        self._cur = cur
        self._lastrowid = lastrowid

    @property
    def lastrowid(self) -> Any:
        return self._lastrowid

    def _wrap(self, row: tuple | None) -> _PgRow | None:
        if row is None:
            return None
        return _PgRow(self._cur.description, row)

    def fetchone(self) -> _PgRow | None:
        if self._cur is None:
            return None
        return self._wrap(self._cur.fetchone())

    def fetchall(self) -> list[_PgRow]:
        if self._cur is None:
            return []
        desc = self._cur.description
        return [_PgRow(desc, r) for r in (self._cur.fetchall() or [])]

    def __iter__(self):
        if self._cur is None:
            return iter([])
        desc = self._cur.description
        for row in self._cur:
            yield _PgRow(desc, row)


# ── Connection wrapper ────────────────────────────────────────────────────────

class _PgConn:
    """Wraps a psycopg connection to look like sqlite3.Connection."""

    def __init__(self, url: str) -> None:
        import psycopg
        self._conn = psycopg.connect(url)
        self._conn.autocommit = False

    def execute(self, sql: str, params: Any = ()) -> _PgCursor:
        adapted, noop = _pg_translate(sql)
        if noop:
            return _PgCursor(None)

        cur = self._conn.cursor()
        is_insert = bool(_RE_INSERT.match(adapted)) and _RE_RETURNING.search(adapted)
        cur.execute(adapted, params or None)

        lastrowid = None
        if is_insert:
            row = cur.fetchone()
            if row is not None:
                lastrowid = row[0]

        return _PgCursor(cur, lastrowid=lastrowid)

    def executemany(self, sql: str, seq: Any) -> _PgCursor:
        adapted, noop = _pg_translate(sql)
        if noop:
            return _PgCursor(None)
        # executemany doesn't need RETURNING; strip it if translation added it
        adapted = _RE_RETURNING.sub("", adapted).rstrip()
        # Ensure ON CONFLICT DO NOTHING lands for INSERT OR IGNORE variants
        if "ON CONFLICT DO NOTHING" not in adapted and _RE_INSERT.match(adapted):
            pass  # plain INSERT — fine
        cur = self._conn.cursor()
        cur.executemany(adapted, seq)
        return _PgCursor(cur)

    def executescript(self, sql: str) -> _PgCursor:
        """Execute multiple SQL statements split on ';'.

        Handles dollar-quoted PL/pgSQL bodies ($$...$$) correctly.
        """
        cur = self._conn.cursor()
        for stmt in _split_statements(sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            adapted, noop = _pg_translate(stmt)
            if noop:
                continue
            # In bulk scripts we skip RETURNING (callers don't use lastrowid here)
            adapted = _RE_RETURNING.sub("", adapted).rstrip().rstrip(";")
            if adapted:
                cur.execute(adapted)
        return _PgCursor(cur)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements on ';'.

    Handles:
    - -- line comments
    - $$ dollar-quoted blocks (postgres PL/pgSQL function bodies)
    """
    stmts: list[str] = []
    current: list[str] = []
    in_dollar = False

    for line in sql.splitlines():
        # Count $$ occurrences to track dollar-quote state
        count = line.count("$$")
        for _ in range(count):
            in_dollar = not in_dollar

        if not in_dollar:
            # Strip line comments outside dollar blocks
            stripped = line.split("--")[0].rstrip()
        else:
            stripped = line

        current.append(stripped)

        if not in_dollar and stripped.rstrip().endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt and stmt != ";":
                stmts.append(stmt)
            current = []

    remainder = "\n".join(current).strip()
    if remainder:
        stmts.append(remainder)
    return stmts


def reset_sequences(con: "_PgConn") -> None:
    """Reset all SERIAL sequences to max(pk) after bulk-loading content.sql.

    Required after INSERT with explicit IDs (content.sql) so that subsequent
    auto-increments don't collide with loaded data.  Only relevant in postgres
    mode; a no-op for sqlite3 connections.
    """
    if not is_postgres():
        return
    tables = [
        ("users", "user_id"),
        ("shells", "shell_id"),
        ("shell_memory_archives", "archive_id"),
        ("shell_identity_entries", "entry_id"),
        ("shell_decisions", "decision_id"),
        ("roadmap", "feature_id"),
        ("documents", "document_id"),
        ("flags", "flag_id"),
        ("spec_tasks", "task_id"),
        ("shell_messages", "message_id"),
        ("skills", "skill_id"),
        ("shell_skills", "shell_skill_id"),
        ("projects", "project_id"),
        ("project_shells", "project_shell_id"),
        ("dr_filepath", "file_id"),
        ("dr_section", "section_id"),
        ("dr_dependency", "dep_id"),
        ("dr_env", "env_id"),
    ]
    cur = con._conn.cursor()
    for table, pk in tables:
        try:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{pk}'), "
                f"COALESCE(MAX({pk}), 0) + 1, false) FROM {table}"
            )
        except Exception:
            con._conn.rollback()
    con.commit()
