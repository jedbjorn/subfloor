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
_FOREIGN_KEYS_OFF = "-- migrate: foreign-keys-off"


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
    sql = path.read_text()
    foreign_keys_off = _FOREIGN_KEYS_OFF in sql
    stamp = path.name.replace("'", "''")
    script = (
        "BEGIN;\n"
        f"{_strip_outer_txn(sql).strip()}\n"
        f"INSERT INTO schema_migrations (filename) VALUES ('{stamp}');\n"
        "COMMIT;"
    )
    try:
        if foreign_keys_off:
            con.execute("PRAGMA foreign_keys=OFF")
        con.executescript(script)
    except Exception:
        con.rollback()
        raise
    finally:
        if foreign_keys_off:
            con.execute("PRAGMA foreign_keys=ON")


def migrate(db_path: str) -> int:
    # Spec #68 req 5: name the two targets — WHICH database, and WHICH migration
    # source — before opening either, and again on the outcome. `./sc migrate`
    # run from a linked worktree used to maintain the main checkout's live DB and
    # print "nothing pending" without ever saying whose DB was current. The
    # disclosure goes first because a crash mid-chain must still leave the
    # operator knowing what was being changed.
    target = Path(db_path).resolve()
    print(f"migrate: db         {target}")
    print(f"migrate: migrations {MIGRATIONS_DIR}")
    con = db_driver.connect(db_path)
    try:
        todo = pending(con)
        if not todo:
            print(f"migrate: nothing pending — {target} is current.")
            return 0
        for path in todo:
            apply(con, path)  # each file self-commits atomically with its stamp
            print(f"migrate: applied {path.name}")
        print(f"migrate: {len(todo)} migration(s) applied to {target}.")
    finally:
        con.close()
    return 0


USAGE = "usage: ./sc migrate  ·  python3 .super-coder/scripts/migrate.py <path-to-db>"

HELP = f"""{USAGE}

Apply every unstamped migration in {MIGRATIONS_DIR.name}/ to the named DB, in
filename order, recording each in the schema_migrations ledger. Reports the
absolute DB path and migration source directory before touching either.

Takes exactly one argument: the path to the database.
"""


def parse_args(argv: list[str]) -> str:
    """Return the DB path, print help, or reject — WITHOUT connecting.

    Help wins over every other token, including a bad one, so no help form can
    reach a database (`./sc migrate --help` used to run the real migration
    against the shared live DB). Same shape as rebuild's preflight (spec #67) —
    the argument contract is settled before any state is opened.
    """
    if "-h" in argv or "--help" in argv:
        print(HELP)
        raise SystemExit(0)
    if len(argv) != 1:
        subject = f"unknown argument '{argv[1]}'" if len(argv) > 1 else "needs a DB path"
        print(f"migrate: {subject} ({USAGE})", file=sys.stderr)
        raise SystemExit(2)
    return argv[0]


if __name__ == "__main__":
    from cli_entry import run_cli

    sys.exit(run_cli(lambda: migrate(parse_args(sys.argv[1:]))))
