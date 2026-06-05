#!/usr/bin/env python3
"""Rebuild the live `shell_db.db` from git-tracked text.

The `.db` is gitignored and disposable. This reconstructs it deterministically:

    1. apply schema.sql            (the v1 baseline)
    2. apply migrations/*.sql      (ordered deltas, via migrate.py; ledger-tracked)
    3. load snapshot/content.sql   (this fork's per-instance content + memory)

If a live DB already exists it is removed first (after an optional backup). The
text serializations are the source of truth; the DB is a cache.

Usage:
    python3 .super-coder/scripts/rebuild.py [--no-backup]
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DB_PATH = ENGINE / "shell_db.db"
SCHEMA = ENGINE / "schema.sql"
SNAPSHOT = ENGINE / "snapshot" / "content.sql"
BACKUP_DIR = Path.home() / "db_backups" / "super-coder"

sys.path.insert(0, str(ENGINE / "scripts"))
import migrate as migrate_mod  # noqa: E402
import map_repo  # noqa: E402


def backup_existing() -> None:
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"shell_db.prerebuild.{ts}.db"
    shutil.copy2(DB_PATH, dst)
    print(f"rebuild: backed up existing DB -> {dst}")


def main(argv: list[str]) -> int:
    if not SCHEMA.exists():
        sys.exit(f"rebuild: missing {SCHEMA}")

    if "--no-backup" not in argv:
        backup_existing()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()

    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(SCHEMA.read_text())
        con.commit()
        print("rebuild: schema.sql applied")
    finally:
        con.close()

    # Apply any migrations layered on top of the baseline.
    migrate_mod.migrate(str(DB_PATH))

    # Load this fork's per-instance content.
    if SNAPSHOT.exists():
        con = sqlite3.connect(DB_PATH)
        try:
            con.executescript(SNAPSHOT.read_text())
            con.commit()
            print(f"rebuild: loaded {SNAPSHOT.relative_to(ENGINE.parent)}")
        finally:
            con.close()
    else:
        print("rebuild: no snapshot/content.sql — built empty (no per-instance content).")

    # The dr_* map is a derived cache, not snapshotted — a fresh DB has an empty
    # one. Refill it here so a bare `./sc rebuild` / `./sc verify` never leaves a
    # shell booting unmapped. Best-effort: the map is a cache, not load-bearing
    # for the rebuild itself. (Hook wiring is map-setup's job, not rebuild's.)
    try:
        map_repo.main()
    except SystemExit as e:  # map_repo.main() sys.exit()s on its own errors
        print(f"rebuild: map skipped ({e}) — run `./sc map` once the repo is ready")
    except Exception as e:  # noqa: BLE001 — never let a map failure fail rebuild
        print(f"rebuild: map failed ({e}) — run `./sc map`")

    size_kb = DB_PATH.stat().st_size / 1024
    print(f"rebuild: done -> {DB_PATH.relative_to(ENGINE.parent)} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
