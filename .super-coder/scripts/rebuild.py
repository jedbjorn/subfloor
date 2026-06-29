#!/usr/bin/env python3
"""Rebuild the super-coder DB from git-tracked text.

SQLite mode (default):
  1. apply schema.sql            (the v1 baseline)
  2. apply migrations/*.sql      (ordered deltas, ledger-tracked)
  3. load .sc-state/content.sql  (per-instance content + memory)

Postgres mode (DATABASE_URL set):
  1. apply schema_pg.sql         (full current baseline — all migrations baked in)
  2. load .sc-state/content.sql  (per-instance content + memory)
  3. reset sequences              (sync SERIAL sequences with loaded data)
  4. stamp all migration filenames as applied (so future updates run only new ones)

The .db file is gitignored and disposable. Text serializations are the source
of truth; the DB is a cache.

Usage:
    python3 .super-coder/scripts/rebuild.py [--no-backup]
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
DB_PATH = ENGINE / "shell_db.db"
SCHEMA_SQLITE = ENGINE / "schema.sql"
SCHEMA_PG     = ENGINE / "schema_pg.sql"
SNAPSHOT       = REPO_ROOT / ".sc-state" / "content.sql"
SNAPSHOT_LEGACY = ENGINE / "snapshot" / "content.sql"
BACKUP_DIR = Path.home() / "db_backups" / "super-coder"

sys.path.insert(0, str(ENGINE / "scripts"))
import db_driver    # noqa: E402
import migrate as migrate_mod  # noqa: E402
import map_repo     # noqa: E402


def snapshot_path() -> Path:
    return SNAPSHOT if SNAPSHOT.exists() else SNAPSHOT_LEGACY


KEEP_BACKUPS = 5


def prune_backups(prefix: str) -> None:
    backups = sorted(BACKUP_DIR.glob(f"{prefix}.*.db"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def backup_existing() -> None:
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"shell_db.prerebuild.{ts}.db"
    shutil.copy2(DB_PATH, dst)
    prune_backups("shell_db.prerebuild")
    print(f"rebuild: backed up existing DB -> {dst}")


def _rebuild_postgres(argv: list[str]) -> int:
    schema = SCHEMA_PG
    if not schema.exists():
        sys.exit(f"rebuild: missing {schema}")

    con = db_driver.connect()
    try:
        con.executescript(schema.read_text())
        con.commit()
        print("rebuild: schema_pg.sql applied")

        snap = snapshot_path()
        if snap.exists():
            con.executescript(snap.read_text())
            con.commit()
            print(f"rebuild: loaded {snap.relative_to(REPO_ROOT)}")
        else:
            print("rebuild: no .sc-state/content.sql — built empty.")

        db_driver.reset_sequences(con)
        print("rebuild: sequences reset")

        migrate_mod.stamp_all(con)
        con.commit()
        print("rebuild: all migrations stamped as applied")
    finally:
        con.close()

    try:
        map_repo.main()
    except SystemExit as e:
        print(f"rebuild: map skipped ({e}) — run `./sc map` once the repo is ready")
    except Exception as e:  # noqa: BLE001
        print(f"rebuild: map failed ({e}) — run `./sc map`")

    print("rebuild: done (postgres)")
    return 0


def main(argv: list[str]) -> int:
    if db_driver.is_postgres():
        return _rebuild_postgres(argv)

    schema = SCHEMA_SQLITE
    if not schema.exists():
        sys.exit(f"rebuild: missing {schema}")

    if "--no-backup" not in argv:
        backup_existing()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()

    con = db_driver.connect(DB_PATH)
    try:
        con.executescript(schema.read_text())
        con.commit()
        print("rebuild: schema.sql applied")
    finally:
        con.close()

    migrate_mod.migrate(str(DB_PATH))

    snap = snapshot_path()
    if snap.exists():
        con = db_driver.connect(DB_PATH)
        try:
            con.executescript(snap.read_text())
            con.commit()
            print(f"rebuild: loaded {snap.relative_to(REPO_ROOT)}")
        finally:
            con.close()
    else:
        print("rebuild: no .sc-state/content.sql — built empty (no per-instance content).")

    try:
        map_repo.main()
    except SystemExit as e:
        print(f"rebuild: map skipped ({e}) — run `./sc map` once the repo is ready")
    except Exception as e:  # noqa: BLE001
        print(f"rebuild: map failed ({e}) — run `./sc map`")

    size_kb = DB_PATH.stat().st_size / 1024
    print(f"rebuild: done -> {DB_PATH.relative_to(ENGINE.parent)} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
