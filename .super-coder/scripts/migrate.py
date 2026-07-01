#!/usr/bin/env python3
"""Apply pending system migrations to a super-coder DB.

Migrations live in `.super-coder/migrations/*.sql`, applied in filename order.
Each applied file is recorded in the `schema_migrations` ledger so it never
runs twice. This is the path a *fork* takes when it pulls super-coder updates:
new migration files appear, `migrate.py` applies only the unstamped ones.

Contract: `schema.sql` is the full current baseline. Every schema change
*after* the baseline is an additive migration here — never folded back into the
schema file (that would double-apply). A fresh build (`rebuild.py`) applies the
schema then calls this to lay every migration down in order.

Usage:
    python3 .super-coder/scripts/migrate.py <path-to-db>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402

# A migration file's own outermost transaction control (on its own line). A
# trigger body's `BEGIN` (no trailing `;`) and `END;` (not `COMMIT;`/`END
# TRANSACTION;`) are deliberately NOT matched, so a `CREATE TRIGGER … BEGIN …
# END;` stays intact when we strip the file's outer BEGIN/COMMIT.
_TXN_BEGIN = re.compile(r"^\s*BEGIN(\s+TRANSACTION)?\s*;\s*$", re.IGNORECASE)
_TXN_COMMIT = re.compile(r"^\s*(COMMIT|END\s+TRANSACTION)\s*;\s*$", re.IGNORECASE)


def applied_set(con) -> set[str]:
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return {r[0] for r in con.execute("SELECT filename FROM schema_migrations")}


def pending(con) -> list[Path]:
    done = applied_set(con)
    files = sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
    return [f for f in files if f.name not in done]


def _strip_outer_txn(sql: str) -> str:
    """Drop the file's own outermost BEGIN (first) and COMMIT (last) so the
    runner can wrap body + ledger stamp in a single transaction without nesting
    (SQLite has no nested transactions). Files that run bare are unchanged."""
    lines = sql.splitlines()
    for i, ln in enumerate(lines):
        if _TXN_BEGIN.match(ln):
            lines[i] = ""
            break
    for i in range(len(lines) - 1, -1, -1):
        if _TXN_COMMIT.match(lines[i]):
            lines[i] = ""
            break
    return "\n".join(lines)


def apply(con, path: Path) -> None:
    """Apply one migration file and stamp the ledger ATOMICALLY.

    executescript() disregards isolation_level and autocommits each statement of
    a bare file, so a mid-file failure used to leave earlier statements applied
    with no ledger row — re-running then re-ran the file from the top and died
    (`duplicate column …`), wedging the chain. Wrapping body + stamp in one
    explicit transaction (with rollback on error) makes a partial failure revert
    whole, leaving the migration unstamped and cleanly re-runnable."""
    stamp = path.name.replace("'", "''")
    script = (
        "BEGIN;\n"
        f"{_strip_outer_txn(path.read_text()).strip()}\n"
        f"INSERT INTO schema_migrations (filename) VALUES ('{stamp}');\n"
        "COMMIT;"
    )
    try:
        con.executescript(script)
    except Exception:
        con.rollback()
        raise


def migrate(db_path: str) -> int:
    con = db_driver.connect(db_path)
    try:
        todo = pending(con)
        if not todo:
            print("migrate: nothing pending — DB is current.")
            return 0
        for path in todo:
            apply(con, path)  # each file self-commits atomically with its stamp
            print(f"migrate: applied {path.name}")
        print(f"migrate: {len(todo)} migration(s) applied.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <path-to-db>")
    sys.exit(migrate(sys.argv[1]))
