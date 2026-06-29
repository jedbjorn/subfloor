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

import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ENGINE / "migrations"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver  # noqa: E402


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


def apply(con, path: Path) -> None:
    con.executescript(path.read_text())
    con.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,))


def migrate(db_path: str) -> int:
    con = db_driver.connect(db_path)
    try:
        todo = pending(con)
        if not todo:
            print("migrate: nothing pending — DB is current.")
            return 0
        for path in todo:
            apply(con, path)
            print(f"migrate: applied {path.name}")
        con.commit()
        print(f"migrate: {len(todo)} migration(s) applied.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <path-to-db>")
    sys.exit(migrate(sys.argv[1]))
